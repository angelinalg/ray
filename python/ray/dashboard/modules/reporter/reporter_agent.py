import asyncio
import datetime
import json
import logging
import os
import requests
import socket
import sys
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple, TypedDict, Union

from opencensus.stats import stats as stats_module
from prometheus_client.core import REGISTRY
from prometheus_client.parser import text_string_to_metric_families
from opentelemetry.proto.collector.metrics.v1 import (
    metrics_service_pb2,
    metrics_service_pb2_grpc,
)
from grpc.aio import ServicerContext


import ray
import ray._private.prometheus_exporter as prometheus_exporter
import ray.dashboard.modules.reporter.reporter_consts as reporter_consts
import ray.dashboard.utils as dashboard_utils
from ray._common.utils import (
    get_or_create_event_loop,
    get_system_memory,
    get_user_temp_dir,
)
from ray._private import utils
from ray._private.metrics_agent import Gauge, MetricsAgent, Record
from ray._private.ray_constants import (
    DEBUG_AUTOSCALING_STATUS,
    RAY_EXPERIMENTAL_ENABLE_OPEN_TELEMETRY_ON_AGENT,
    RAY_EXPERIMENTAL_ENABLE_OPEN_TELEMETRY_ON_CORE,
    env_integer,
)
from ray._private.telemetry.open_telemetry_metric_recorder import (
    OpenTelemetryMetricRecorder,
)
from ray._raylet import GCS_PID_KEY, WorkerID
from ray.core.generated import reporter_pb2, reporter_pb2_grpc
from ray.dashboard import k8s_utils
from ray.dashboard.consts import (
    CLUSTER_TAG_KEYS,
    COMPONENT_METRICS_TAG_KEYS,
    GCS_RPC_TIMEOUT_SECONDS,
    GPU_TAG_KEYS,
    TPU_TAG_KEYS,
    NODE_TAG_KEYS,
)
from ray.dashboard.modules.reporter.gpu_profile_manager import GpuProfilingManager
from ray.dashboard.modules.reporter.profile_manager import (
    CpuProfilingManager,
    MemoryProfilingManager,
)

import psutil

logger = logging.getLogger(__name__)

enable_gpu_usage_check = True

enable_tpu_usage_check = True

# Are we in a K8s pod?
IN_KUBERNETES_POD = "KUBERNETES_SERVICE_HOST" in os.environ
# Flag to enable showing disk usage when running in a K8s pod,
# disk usage defined as the result of running psutil.disk_usage("/")
# in the Ray container.
ENABLE_K8S_DISK_USAGE = os.environ.get("RAY_DASHBOARD_ENABLE_K8S_DISK_USAGE") == "1"
# Try to determine if we're in a container.
IN_CONTAINER = os.path.exists("/sys/fs/cgroup")
# Using existence of /sys/fs/cgroup as the criterion is consistent with
# Ray's existing resource logic, see e.g. ray._private.utils.get_num_cpus().

# NOTE: Executor in this head is intentionally constrained to just 1 thread by
#       default to limit its concurrency, therefore reducing potential for
#       GIL contention
RAY_DASHBOARD_REPORTER_AGENT_TPE_MAX_WORKERS = env_integer(
    "RAY_DASHBOARD_REPORTER_AGENT_TPE_MAX_WORKERS", 1
)

# TPU device plugin metric address should be in the format "{HOST_IP}:2112"
TPU_DEVICE_PLUGIN_ADDR = os.environ.get("TPU_DEVICE_PLUGIN_ADDR", None)


def recursive_asdict(o):
    if isinstance(o, tuple) and hasattr(o, "_asdict"):
        return recursive_asdict(o._asdict())

    if isinstance(o, (tuple, list)):
        L = []
        for k in o:
            L.append(recursive_asdict(k))
        return L

    if isinstance(o, dict):
        D = {k: recursive_asdict(v) for k, v in o.items()}
        return D

    return o


def jsonify_asdict(o) -> str:
    return json.dumps(dashboard_utils.to_google_style(recursive_asdict(o)))


# A list of gauges to record and export metrics.
METRICS_GAUGES = {
    # CPU metrics
    "node_cpu_utilization": Gauge(
        "node_cpu_utilization",
        "Total CPU usage on a ray node",
        "percentage",
        NODE_TAG_KEYS,
    ),
    "node_cpu_count": Gauge(
        "node_cpu_count",
        "Total CPUs available on a ray node",
        "cores",
        NODE_TAG_KEYS,
    ),
    # Memory metrics
    "node_mem_used": Gauge(
        "node_mem_used",
        "Memory usage on a ray node",
        "bytes",
        NODE_TAG_KEYS,
    ),
    "node_mem_available": Gauge(
        "node_mem_available",
        "Memory available on a ray node",
        "bytes",
        NODE_TAG_KEYS,
    ),
    "node_mem_total": Gauge(
        "node_mem_total",
        "Total memory on a ray node",
        "bytes",
        NODE_TAG_KEYS,
    ),
    "node_mem_shared_bytes": Gauge(
        "node_mem_shared_bytes",
        "Total shared memory usage on a ray node",
        "bytes",
        NODE_TAG_KEYS,
    ),
    # GPU metrics
    "node_gpus_available": Gauge(
        "node_gpus_available",
        "Total GPUs available on a ray node",
        "percentage",
        GPU_TAG_KEYS,
    ),
    "node_gpus_utilization": Gauge(
        "node_gpus_utilization",
        "Total GPUs usage on a ray node",
        "percentage",
        GPU_TAG_KEYS,
    ),
    "node_gram_used": Gauge(
        "node_gram_used",
        "Total GPU RAM usage on a ray node",
        "bytes",
        GPU_TAG_KEYS,
    ),
    "node_gram_available": Gauge(
        "node_gram_available",
        "Total GPU RAM available on a ray node",
        "bytes",
        GPU_TAG_KEYS,
    ),
    # TPU metrics
    "tpu_tensorcore_utilization": Gauge(
        "tpu_tensorcore_utilization",
        "Percentage TPU tensorcore utilization on a ray node, value should be between 0 and 100",
        "percentage",
        TPU_TAG_KEYS,
    ),
    "tpu_memory_bandwidth_utilization": Gauge(
        "tpu_memory_bandwidth_utilization",
        "Percentage TPU memory bandwidth utilization on a ray node, value should be between 0 and 100",
        "percentage",
        TPU_TAG_KEYS,
    ),
    "tpu_duty_cycle": Gauge(
        "tpu_duty_cycle",
        "Percentage of time during which the TPU was actively processing, value should be between 0 and 100",
        "percentage",
        TPU_TAG_KEYS,
    ),
    "tpu_memory_used": Gauge(
        "tpu_memory_used",
        "Total memory used by the accelerator in bytes",
        "bytes",
        TPU_TAG_KEYS,
    ),
    "tpu_memory_total": Gauge(
        "tpu_memory_total",
        "Total memory allocatable by the accelerator in bytes",
        "bytes",
        TPU_TAG_KEYS,
    ),
    # Disk I/O metrics
    "node_disk_io_read": Gauge(
        "node_disk_io_read",
        "Total read from disk",
        "bytes",
        NODE_TAG_KEYS,
    ),
    "node_disk_io_write": Gauge(
        "node_disk_io_write",
        "Total written to disk",
        "bytes",
        NODE_TAG_KEYS,
    ),
    "node_disk_io_read_count": Gauge(
        "node_disk_io_read_count",
        "Total read ops from disk",
        "io",
        NODE_TAG_KEYS,
    ),
    "node_disk_io_write_count": Gauge(
        "node_disk_io_write_count",
        "Total write ops to disk",
        "io",
        NODE_TAG_KEYS,
    ),
    "node_disk_io_read_speed": Gauge(
        "node_disk_io_read_speed",
        "Disk read speed",
        "bytes/sec",
        NODE_TAG_KEYS,
    ),
    "node_disk_io_write_speed": Gauge(
        "node_disk_io_write_speed",
        "Disk write speed",
        "bytes/sec",
        NODE_TAG_KEYS,
    ),
    "node_disk_read_iops": Gauge(
        "node_disk_read_iops",
        "Disk read iops",
        "iops",
        NODE_TAG_KEYS,
    ),
    "node_disk_write_iops": Gauge(
        "node_disk_write_iops",
        "Disk write iops",
        "iops",
        NODE_TAG_KEYS,
    ),
    # Disk usage metrics
    "node_disk_usage": Gauge(
        "node_disk_usage",
        "Total disk usage (bytes) on a ray node",
        "bytes",
        NODE_TAG_KEYS,
    ),
    "node_disk_free": Gauge(
        "node_disk_free",
        "Total disk free (bytes) on a ray node",
        "bytes",
        NODE_TAG_KEYS,
    ),
    "node_disk_utilization_percentage": Gauge(
        "node_disk_utilization_percentage",
        "Total disk utilization (percentage) on a ray node",
        "percentage",
        NODE_TAG_KEYS,
    ),
    # Network metrics
    "node_network_sent": Gauge(
        "node_network_sent",
        "Total network sent",
        "bytes",
        NODE_TAG_KEYS,
    ),
    "node_network_received": Gauge(
        "node_network_received",
        "Total network received",
        "bytes",
        NODE_TAG_KEYS,
    ),
    "node_network_send_speed": Gauge(
        "node_network_send_speed",
        "Network send speed",
        "bytes/sec",
        NODE_TAG_KEYS,
    ),
    "node_network_receive_speed": Gauge(
        "node_network_receive_speed",
        "Network receive speed",
        "bytes/sec",
        NODE_TAG_KEYS,
    ),
    # Component metrics
    "component_cpu_percentage": Gauge(
        "component_cpu_percentage",
        "Total CPU usage of the components on a node.",
        "percentage",
        COMPONENT_METRICS_TAG_KEYS,
    ),
    "component_mem_shared_bytes": Gauge(
        "component_mem_shared_bytes",
        "SHM usage of all components of the node. "
        "It is equivalent to the top command's SHR column.",
        "bytes",
        COMPONENT_METRICS_TAG_KEYS,
    ),
    "component_rss_mb": Gauge(
        "component_rss_mb",
        "RSS usage of all components on the node.",
        "MB",
        COMPONENT_METRICS_TAG_KEYS,
    ),
    "component_uss_mb": Gauge(
        "component_uss_mb",
        "USS usage of all components on the node.",
        "MB",
        COMPONENT_METRICS_TAG_KEYS,
    ),
    "component_num_fds": Gauge(
        "component_num_fds",
        "Number of open fds of all components on the node (Not available on Windows).",
        "count",
        COMPONENT_METRICS_TAG_KEYS,
    ),
    # Cluster metrics
    "cluster_active_nodes": Gauge(
        "cluster_active_nodes",
        "Active nodes on the cluster",
        "count",
        CLUSTER_TAG_KEYS,
    ),
    "cluster_failed_nodes": Gauge(
        "cluster_failed_nodes",
        "Failed nodes on the cluster",
        "count",
        CLUSTER_TAG_KEYS,
    ),
    "cluster_pending_nodes": Gauge(
        "cluster_pending_nodes",
        "Pending nodes on the cluster",
        "count",
        CLUSTER_TAG_KEYS,
    ),
}

PSUTIL_PROCESS_ATTRS = (
    [
        "pid",
        "create_time",
        "cpu_percent",
        "cpu_times",
        "cmdline",
        "memory_info",
        "memory_full_info",
    ]
    + ["num_fds"]
    if sys.platform != "win32"
    else []
)

MB = 1024 * 1024

# Types
Percentage = int
Megabytes = int
Bytes = int


# gpu utilization for nvidia gpu from a single process
class ProcessGPUInfo(TypedDict):
    pid: int
    gpu_memory_usage: Megabytes


# gpu utilization for nvidia gpu
class GpuUtilizationInfo(TypedDict):
    index: int
    name: str
    uuid: str
    utilization_gpu: Optional[Percentage]
    memory_used: Megabytes
    memory_total: Megabytes
    processes_pids: Optional[List[ProcessGPUInfo]]


# tpu utilization for google tpu
class TpuUtilizationInfo(TypedDict):
    index: int
    name: str
    tpu_type: str
    tpu_topology: str
    tensorcore_utilization: Percentage
    hbm_utilization: Percentage
    duty_cycle: Percentage
    memory_used: Bytes
    memory_total: Bytes


class ReporterAgent(
    dashboard_utils.DashboardAgentModule,
    reporter_pb2_grpc.ReporterServiceServicer,
    metrics_service_pb2_grpc.MetricsServiceServicer,
):
    """A monitor process for monitoring Ray nodes.

    Attributes:
        dashboard_agent: The DashboardAgent object contains global config
    """

    def __init__(self, dashboard_agent):
        """Initialize the reporter object."""
        super().__init__(dashboard_agent)

        if IN_KUBERNETES_POD or IN_CONTAINER:
            # psutil does not give a meaningful logical cpu count when in a K8s pod, or
            # in a container in general.
            # Use ray._private.utils for this instead.
            logical_cpu_count = utils.get_num_cpus(override_docker_cpu_warning=True)
            # (Override the docker warning to avoid dashboard log spam.)

            # The dashboard expects a physical CPU count as well.
            # This is not always meaningful in a container, but we will go ahead
            # and give the dashboard what it wants using psutil.
            physical_cpu_count = psutil.cpu_count(logical=False)
        else:
            logical_cpu_count = psutil.cpu_count()
            physical_cpu_count = psutil.cpu_count(logical=False)
        self._cpu_counts = (logical_cpu_count, physical_cpu_count)
        self._gcs_client = dashboard_agent.gcs_client
        self._ip = dashboard_agent.ip
        self._log_dir = dashboard_agent.log_dir
        self._is_head_node = self._ip == dashboard_agent.gcs_address.split(":")[0]
        self._hostname = socket.gethostname()
        # (pid, created_time) -> psutil.Process
        self._workers = {}
        # psutil.Process of the parent.
        self._raylet_proc = None
        # psutil.Process of the current process.
        self._agent_proc = None
        # The last reported worker proc names (e.g., ray::*).
        self._latest_worker_proc_names = set()
        self._network_stats_hist = [(0, (0.0, 0.0))]  # time, (sent, recv)
        self._disk_io_stats_hist = [
            (0, (0.0, 0.0, 0, 0))
        ]  # time, (bytes read, bytes written, read ops, write ops)
        self._metrics_collection_disabled = dashboard_agent.metrics_collection_disabled
        self._metrics_agent = None
        self._open_telemetry_metric_recorder = None
        self._session_name = dashboard_agent.session_name
        if not self._metrics_collection_disabled:
            try:
                stats_exporter = prometheus_exporter.new_stats_exporter(
                    prometheus_exporter.Options(
                        namespace="ray",
                        port=dashboard_agent.metrics_export_port,
                        address="127.0.0.1" if self._ip == "127.0.0.1" else "",
                    )
                )
            except Exception:
                # TODO(SongGuyang): Catch the exception here because there is
                # port conflict issue which brought from static port. We should
                # remove this after we find better port resolution.
                logger.exception(
                    "Failed to start prometheus stats exporter. Agent will stay "
                    "alive but disable the stats."
                )
                stats_exporter = None

            self._metrics_agent = MetricsAgent(
                stats_module.stats.view_manager,
                stats_module.stats.stats_recorder,
                stats_exporter,
            )
            self._open_telemetry_metric_recorder = OpenTelemetryMetricRecorder()
            if self._metrics_agent.proxy_exporter_collector:
                # proxy_exporter_collector is None
                # if Prometheus server is not started.
                REGISTRY.register(self._metrics_agent.proxy_exporter_collector)
        self._key = (
            f"{reporter_consts.REPORTER_PREFIX}" f"{self._dashboard_agent.node_id}"
        )

        self._executor = ThreadPoolExecutor(
            max_workers=RAY_DASHBOARD_REPORTER_AGENT_TPE_MAX_WORKERS,
            thread_name_prefix="reporter_agent_executor",
        )
        self._gcs_pid = None

        self._gpu_profiling_manager = GpuProfilingManager(
            profile_dir_path=self._log_dir, ip_address=self._ip
        )
        self._gpu_profiling_manager.start_monitoring_daemon()

    async def GetTraceback(self, request, context):
        pid = request.pid
        native = request.native
        p = CpuProfilingManager(self._log_dir)
        success, output = await p.trace_dump(pid, native=native)
        return reporter_pb2.GetTracebackReply(output=output, success=success)

    async def CpuProfiling(self, request, context):
        pid = request.pid
        duration = request.duration
        format = request.format
        native = request.native
        p = CpuProfilingManager(self._log_dir)
        success, output = await p.cpu_profile(
            pid, format=format, duration=duration, native=native
        )
        return reporter_pb2.CpuProfilingReply(output=output, success=success)

    async def GpuProfiling(self, request, context):
        pid = request.pid
        num_iterations = request.num_iterations
        success, output = await self._gpu_profiling_manager.gpu_profile(
            pid=pid, num_iterations=num_iterations
        )
        return reporter_pb2.GpuProfilingReply(success=success, output=output)

    async def MemoryProfiling(self, request, context):
        pid = request.pid
        format = request.format
        leaks = request.leaks
        duration = request.duration
        native = request.native
        trace_python_allocators = request.trace_python_allocators
        p = MemoryProfilingManager(self._log_dir)
        success, profiler_filename, output = await p.attach_profiler(
            pid, native=native, trace_python_allocators=trace_python_allocators
        )
        if not success:
            return reporter_pb2.MemoryProfilingReply(output=output, success=success)

        # add 1 second sleep for memray overhead
        await asyncio.sleep(duration + 1)
        success, output = await p.detach_profiler(pid)
        warning = None if success else output
        success, output = await p.get_profile_result(
            pid, profiler_filename=profiler_filename, format=format, leaks=leaks
        )
        return reporter_pb2.MemoryProfilingReply(
            output=output, success=success, warning=warning
        )

    async def ReportOCMetrics(self, request, context):
        # Do nothing if metrics collection is disabled.
        if self._metrics_collection_disabled:
            return reporter_pb2.ReportOCMetricsReply()

        # This function receives a GRPC containing OpenCensus (OC) metrics
        # from a Ray process, then exposes those metrics to Prometheus.
        try:
            worker_id = WorkerID(request.worker_id)
            worker_id = None if worker_id.is_nil() else worker_id.hex()
            self._metrics_agent.proxy_export_metrics(request.metrics, worker_id)
        except Exception:
            logger.error(traceback.format_exc())
        return reporter_pb2.ReportOCMetricsReply()

    async def Export(
        self,
        request: metrics_service_pb2.ExportMetricsServiceRequest,
        context: ServicerContext,
    ) -> metrics_service_pb2.ExportMetricsServiceResponse:
        """
        GRPC method that receives the open telemetry metrics exported from other Ray
        components running in the same node (e.g., raylet, worker, etc.). This method
        implements an interface of `metrics_service_pb2_grpc.MetricsServiceServicer` (https://github.com/open-telemetry/opentelemetry-proto/blob/main/opentelemetry/proto/collector/metrics/v1/metrics_service.proto#L30),
        which is the default open-telemetry metrics service interface.
        """
        for resource_metrics in request.resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    data_points = []
                    # gauge metrics
                    if metric.WhichOneof("data") == "gauge":
                        self._open_telemetry_metric_recorder.register_gauge_metric(
                            metric.name, metric.description or ""
                        )
                        data_points = metric.gauge.data_points
                    # counter metrics
                    if metric.WhichOneof("data") == "sum" and metric.sum.is_monotonic:
                        self._open_telemetry_metric_recorder.register_counter_metric(
                            metric.name, metric.description or ""
                        )
                        data_points = metric.sum.data_points
                    # sum metrics
                    if (
                        metric.WhichOneof("data") == "sum"
                        and not metric.sum.is_monotonic
                    ):
                        self._open_telemetry_metric_recorder.register_sum_metric(
                            metric.name, metric.description or ""
                        )
                        data_points = metric.sum.data_points
                    for data_point in data_points:
                        self._open_telemetry_metric_recorder.set_metric_value(
                            metric.name,
                            {
                                tag.key: tag.value.string_value
                                for tag in data_point.attributes
                            },
                            # Note that all data points received from other Ray
                            # components are always double values. This is because the
                            # c++ apis (open_telemetry_metric_recorder.cc) only create
                            # metrics with double values.
                            data_point.as_double,
                        )

        return metrics_service_pb2.ExportMetricsServiceResponse()

    @staticmethod
    def _get_cpu_percent(in_k8s: bool):
        if in_k8s:
            return k8s_utils.cpu_percent()
        else:
            return psutil.cpu_percent()

    @staticmethod
    def _get_gpu_usage():
        import ray._private.thirdparty.pynvml as pynvml

        global enable_gpu_usage_check
        if not enable_gpu_usage_check:
            return []
        gpu_utilizations = []

        def decode(b: Union[str, bytes]) -> str:
            if isinstance(b, bytes):
                return b.decode("utf-8")  # for python3, to unicode
            return b

        try:
            pynvml.nvmlInit()
        except Exception as e:
            logger.debug(f"pynvml failed to retrieve GPU information: {e}")

            # On machines without GPUs, pynvml.nvmlInit() can run subprocesses that
            # spew to stderr. Then with log_to_driver=True, we get log spew from every
            # single raylet. To avoid this, disable the GPU usage check on
            # certain errors.
            # https://github.com/ray-project/ray/issues/14305
            # https://github.com/ray-project/ray/pull/21686
            if type(e).__name__ == "NVMLError_DriverNotLoaded":
                enable_gpu_usage_check = False
            return gpu_utilizations

        num_gpus = pynvml.nvmlDeviceGetCount()
        for i in range(num_gpus):
            gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            memory_info = pynvml.nvmlDeviceGetMemoryInfo(gpu_handle)
            utilization = None
            try:
                utilization_info = pynvml.nvmlDeviceGetUtilizationRates(gpu_handle)
                utilization = int(utilization_info.gpu)
            except pynvml.NVMLError as e:
                logger.debug(f"pynvml failed to retrieve GPU utilization: {e}")

            # processes pids
            processes_pids = None
            try:
                nv_comp_processes = pynvml.nvmlDeviceGetComputeRunningProcesses(
                    gpu_handle
                )
                nv_graphics_processes = pynvml.nvmlDeviceGetGraphicsRunningProcesses(
                    gpu_handle
                )
                processes_pids = [
                    ProcessGPUInfo(
                        pid=int(nv_process.pid),
                        gpu_memory_usage=(
                            int(nv_process.usedGpuMemory) // MB
                            if nv_process.usedGpuMemory
                            else 0
                        ),
                    )
                    for nv_process in (nv_comp_processes + nv_graphics_processes)
                ]
            except pynvml.NVMLError as e:
                logger.debug(f"pynvml failed to retrieve GPU processes: {e}")

            info = GpuUtilizationInfo(
                index=i,
                name=decode(pynvml.nvmlDeviceGetName(gpu_handle)),
                uuid=decode(pynvml.nvmlDeviceGetUUID(gpu_handle)),
                utilization_gpu=utilization,
                memory_used=int(memory_info.used) // MB,
                memory_total=int(memory_info.total) // MB,
                processes_pids=processes_pids,
            )
            gpu_utilizations.append(info)
        pynvml.nvmlShutdown()

        return gpu_utilizations

    @staticmethod
    def _get_tpu_usage() -> List[TpuUtilizationInfo]:

        global enable_tpu_usage_check
        if not enable_tpu_usage_check:
            return []

        if not TPU_DEVICE_PLUGIN_ADDR:
            enable_tpu_usage_check = False
            return []

        endpoint = f"http://{TPU_DEVICE_PLUGIN_ADDR}/metrics"
        try:
            metrics = requests.get(endpoint).content
            metrics = metrics.decode("utf-8")
        except Exception as e:
            logger.debug(
                f"Failed to retrieve TPU information from device plugin: {endpoint} {e}"
            )
            enable_tpu_usage_check = False
            return []

        tpu_utilizations = []
        # Sample should look like:
        # Name: tensorcore_utilization_node Labels: {'accelerator_id': '4804690994094478883-0', 'make': 'cloud-tpu', 'model': 'tpu-v6e-slice', 'tpu_topology': '2x4'} Value: 0.0
        # See https://cloud.google.com/monitoring/api/metrics_gcp#gcp-tpu for
        # schema.
        try:
            for family in text_string_to_metric_families(metrics):
                for sample in family.samples:
                    # Skip irrelevant metrics
                    if not hasattr(sample, "labels"):
                        continue
                    if "accelerator_id" not in sample.labels:
                        continue
                    labels = sample.labels
                    accelerator_id = labels["accelerator_id"]
                    index = accelerator_id.split("-")[1]

                    if sample.name == "memory_bandwidth_utilization":
                        info = TpuUtilizationInfo(
                            index=index,
                            name=accelerator_id,
                            tpu_type=labels["model"],
                            tpu_topology=labels["tpu_topology"],
                            tensorcore_utilization=0.0,
                            hbm_utilization=sample.value,
                            duty_cycle=0.0,
                            memory_used=0,
                            memory_total=0,
                        )
                        tpu_utilizations.append(info)

                    if sample.name == "tensorcore_utilization":
                        info = TpuUtilizationInfo(
                            index=index,
                            name=accelerator_id,
                            tpu_type=labels["model"],
                            tpu_topology=labels["tpu_topology"],
                            tensorcore_utilization=sample.value,
                            hbm_utilization=0.0,
                            duty_cycle=0.0,
                            memory_used=0,
                            memory_total=0,
                        )
                        tpu_utilizations.append(info)

                    if sample.name == "duty_cycle":
                        info = TpuUtilizationInfo(
                            index=index,
                            name=accelerator_id,
                            tpu_type=labels["model"],
                            tpu_topology=labels["tpu_topology"],
                            tensorcore_utilization=0.0,
                            hbm_utilization=0.0,
                            duty_cycle=sample.value,
                            memory_used=0,
                            memory_total=0,
                        )
                        tpu_utilizations.append(info)

                    if sample.name == "memory_used":
                        info = TpuUtilizationInfo(
                            index=index,
                            name=accelerator_id,
                            tpu_type=labels["model"],
                            tpu_topology=labels["tpu_topology"],
                            tensorcore_utilization=0.0,
                            hbm_utilization=0.0,
                            duty_cycle=0.0,
                            memory_used=sample.value,
                            memory_total=0,
                        )
                        tpu_utilizations.append(info)

                    if sample.name == "memory_total":
                        info = TpuUtilizationInfo(
                            index=index,
                            name=accelerator_id,
                            tpu_type=labels["model"],
                            tpu_topology=labels["tpu_topology"],
                            tensorcore_utilization=0.0,
                            hbm_utilization=0.0,
                            duty_cycle=0.0,
                            memory_used=0,
                            memory_total=sample.value,
                        )
                        tpu_utilizations.append(info)
        except Exception as e:
            logger.debug(f"Failed to parse metrics from device plugin: {metrics} {e}")
            return []

        # Each collected sample records only one metric (e.g. duty cycle) during
        # the metric interval for one TPU. So here we need to aggregate the
        # sample records together. The aggregated list should be indexed by the
        # TPU accelerator index.
        merged_tpu_utilizations = {}

        for info in tpu_utilizations:
            index = int(info.get("index"))
            if index in merged_tpu_utilizations:
                merged_info = merged_tpu_utilizations[index]
                merged_info["tensorcore_utilization"] += info.get(
                    "tensorcore_utilization"
                )
                merged_info["hbm_utilization"] += info.get("hbm_utilization")
                merged_info["duty_cycle"] += info.get("duty_cycle")
                merged_info["memory_used"] += info.get("memory_used")
                merged_info["memory_total"] += info.get("memory_total")
            else:
                merged_info = TpuUtilizationInfo(
                    index=info.get("index"),
                    name=info.get("name"),
                    tpu_type=info.get("tpu_type"),
                    tpu_topology=info.get("tpu_topology"),
                    tensorcore_utilization=info.get("tensorcore_utilization"),
                    hbm_utilization=info.get("hbm_utilization"),
                    duty_cycle=info.get("duty_cycle"),
                    memory_used=info.get("memory_used"),
                    memory_total=info.get("memory_total"),
                )
                merged_tpu_utilizations[index] = merged_info

        sorted_tpu_utilizations = [
            value for _, value in sorted(merged_tpu_utilizations.items())
        ]
        return sorted_tpu_utilizations

    @staticmethod
    def _get_boot_time():
        if IN_KUBERNETES_POD:
            # Return start time of container entrypoint
            return psutil.Process(pid=1).create_time()
        else:
            return psutil.boot_time()

    @staticmethod
    def _get_network_stats():
        ifaces = [
            v for k, v in psutil.net_io_counters(pernic=True).items() if k[0] == "e"
        ]

        sent = sum((iface.bytes_sent for iface in ifaces))
        recv = sum((iface.bytes_recv for iface in ifaces))
        return sent, recv

    @staticmethod
    def _get_mem_usage():
        total = get_system_memory()
        used = utils.get_used_memory()
        available = total - used
        percent = round(used / total, 3) * 100
        return total, available, percent, used

    @staticmethod
    def _get_disk_usage():
        if IN_KUBERNETES_POD and not ENABLE_K8S_DISK_USAGE:
            # If in a K8s pod, disable disk display by passing in dummy values.
            return {
                "/": psutil._common.sdiskusage(total=1, used=0, free=1, percent=0.0)
            }
        if sys.platform == "win32":
            root = psutil.disk_partitions()[0].mountpoint
        else:
            root = os.sep
        tmp = get_user_temp_dir()
        return {
            "/": psutil.disk_usage(root),
            tmp: psutil.disk_usage(tmp),
        }

    @staticmethod
    def _get_disk_io_stats():
        stats = psutil.disk_io_counters()
        # stats can be None or {} if the machine is diskless.
        # https://psutil.readthedocs.io/en/latest/#psutil.disk_io_counters
        if not stats:
            return (0, 0, 0, 0)
        else:
            return (
                stats.read_bytes,
                stats.write_bytes,
                stats.read_count,
                stats.write_count,
            )

    def _get_agent_proc(self) -> psutil.Process:
        # Agent is the current process.
        # This method is not necessary, but we have it for mock testing.
        return psutil.Process()

    def _generate_worker_key(self, proc: psutil.Process) -> Tuple[int, float]:
        return (proc.pid, proc.create_time())

    def _get_workers(self):
        raylet_proc = self._get_raylet_proc()

        if raylet_proc is None:
            return []
        else:
            workers = {}
            if sys.platform == "win32":
                # windows, get the child process not the runner
                for child in raylet_proc.children():
                    if child.children():
                        child = child.children()[0]
                    workers[self._generate_worker_key(child)] = child
            else:
                workers = {
                    self._generate_worker_key(proc): proc
                    for proc in raylet_proc.children()
                }

            # We should keep `raylet_proc.children()` in `self` because
            # when `cpu_percent` is first called, it returns the meaningless 0.
            # See more: https://github.com/ray-project/ray/issues/29848
            keys_to_pop = []
            # Add all new workers.
            for key, worker in workers.items():
                if key not in self._workers:
                    self._workers[key] = worker

            # Pop out stale workers.
            for key in self._workers:
                if key not in workers:
                    keys_to_pop.append(key)
            for k in keys_to_pop:
                self._workers.pop(k)

            # Remove the current process (reporter agent), which is also a child of
            # the Raylet.
            self._workers.pop(self._generate_worker_key(self._get_agent_proc()))

            result = []
            for w in self._workers.values():
                try:
                    if w.status() == psutil.STATUS_ZOMBIE:
                        continue
                    result.append(w.as_dict(attrs=PSUTIL_PROCESS_ATTRS))
                except psutil.NoSuchProcess:
                    # the process may have terminated due to race condition.
                    continue

            return result

    def _get_raylet_proc(self):
        try:
            if not self._raylet_proc:
                curr_proc = psutil.Process()
                # The dashboard agent is a child of the raylet process.
                # It is not necessarily the direct child (python-windows
                # typically uses a py.exe runner to run python), so search
                # up for a process named 'raylet'
                candidate = curr_proc.parent()
                while candidate:
                    if "raylet" in candidate.name():
                        break
                    candidate = candidate.parent()
                self._raylet_proc = candidate

            if self._raylet_proc is not None:
                if self._raylet_proc.pid == 1:
                    return None
                if self._raylet_proc.status() == psutil.STATUS_ZOMBIE:
                    return None
            return self._raylet_proc
        except (psutil.AccessDenied, ProcessLookupError):
            pass
        return None

    def _get_gcs(self):
        if self._gcs_pid:
            gcs_proc = psutil.Process(self._gcs_pid)
            if gcs_proc:
                return gcs_proc.as_dict(attrs=PSUTIL_PROCESS_ATTRS)
        return {}

    def _get_raylet(self):
        raylet_proc = self._get_raylet_proc()
        if raylet_proc is None:
            return {}
        else:
            return raylet_proc.as_dict(attrs=PSUTIL_PROCESS_ATTRS)

    def _get_agent(self):
        # Current proc == agent proc
        if not self._agent_proc:
            self._agent_proc = psutil.Process()
        return self._agent_proc.as_dict(attrs=PSUTIL_PROCESS_ATTRS)

    def _get_load_avg(self):
        if sys.platform == "win32":
            cpu_percent = psutil.cpu_percent()
            load = (cpu_percent, cpu_percent, cpu_percent)
        else:
            load = os.getloadavg()
        if self._cpu_counts[0] > 0:
            per_cpu_load = tuple((round(x / self._cpu_counts[0], 2) for x in load))
        else:
            per_cpu_load = None
        return load, per_cpu_load

    @staticmethod
    def _compute_speed_from_hist(hist):
        while len(hist) > 7:
            hist.pop(0)
        then, prev_stats = hist[0]
        now, now_stats = hist[-1]
        time_delta = now - then
        return tuple((y - x) / time_delta for x, y in zip(prev_stats, now_stats))

    def _get_shm_usage(self):
        """Return the shm usage.

        If shm doesn't exist (e.g., MacOS), it returns None.
        """
        mem = psutil.virtual_memory()
        if not hasattr(mem, "shared"):
            return None
        return mem.shared

    def _collect_stats(self):
        now = dashboard_utils.to_posix_time(datetime.datetime.utcnow())
        network_stats = self._get_network_stats()
        self._network_stats_hist.append((now, network_stats))
        network_speed_stats = self._compute_speed_from_hist(self._network_stats_hist)

        disk_stats = self._get_disk_io_stats()
        self._disk_io_stats_hist.append((now, disk_stats))
        disk_speed_stats = self._compute_speed_from_hist(self._disk_io_stats_hist)

        stats = {
            "now": now,
            "hostname": self._hostname,
            "ip": self._ip,
            "cpu": self._get_cpu_percent(IN_KUBERNETES_POD),
            "cpus": self._cpu_counts,
            "mem": self._get_mem_usage(),
            # Unit is in bytes. None if
            "shm": self._get_shm_usage(),
            "workers": self._get_workers(),
            "raylet": self._get_raylet(),
            "agent": self._get_agent(),
            "bootTime": self._get_boot_time(),
            "loadAvg": self._get_load_avg(),
            "disk": self._get_disk_usage(),
            "disk_io": disk_stats,
            "disk_io_speed": disk_speed_stats,
            "gpus": self._get_gpu_usage(),
            "tpus": self._get_tpu_usage(),
            "network": network_stats,
            "network_speed": network_speed_stats,
            # Deprecated field, should be removed with frontend.
            "cmdline": self._get_raylet().get("cmdline", []),
        }
        if self._is_head_node:
            stats["gcs"] = self._get_gcs()
        return stats

    def _generate_reseted_stats_record(self, component_name: str) -> List[Record]:
        """Return a list of Record that will reset
        the system metrics of a given component name.

        Args:
            component_name: a component name for a given stats.

        Returns:
            a list of Record instances of all values 0.
        """
        tags = {"ip": self._ip, "Component": component_name}

        records = []
        records.append(
            Record(
                gauge=METRICS_GAUGES["component_cpu_percentage"],
                value=0.0,
                tags=tags,
            )
        )
        records.append(
            Record(
                gauge=METRICS_GAUGES["component_mem_shared_bytes"],
                value=0.0,
                tags=tags,
            )
        )
        records.append(
            Record(
                gauge=METRICS_GAUGES["component_rss_mb"],
                value=0.0,
                tags=tags,
            )
        )
        records.append(
            Record(
                gauge=METRICS_GAUGES["component_uss_mb"],
                value=0.0,
                tags=tags,
            )
        )
        records.append(
            Record(
                gauge=METRICS_GAUGES["component_num_fds"],
                value=0,
                tags=tags,
            )
        )
        return records

    def _generate_system_stats_record(
        self, stats: List[dict], component_name: str, pid: Optional[str] = None
    ) -> List[Record]:
        """Generate a list of Record class from a given component names.

        Args:
            stats: a list of stats dict generated by `psutil.as_dict`.
                If empty, it will create the metrics of a given "component_name"
                which has all 0 values.
            component_name: a component name for a given stats.
            pid: optionally provided pids.

        Returns:
            a list of Record class that will be exposed to Prometheus.
        """
        total_cpu_percentage = 0.0
        total_rss = 0.0
        total_uss = 0.0
        total_shm = 0.0
        total_num_fds = 0

        for stat in stats:
            total_cpu_percentage += float(stat.get("cpu_percent", 0.0))  # noqa
            memory_info = stat.get("memory_info")
            if memory_info:
                mem = stat["memory_info"]
                total_rss += float(mem.rss) / 1.0e6
                if hasattr(mem, "shared"):
                    total_shm += float(mem.shared)
            mem_full_info = stat.get("memory_full_info")
            if mem_full_info is not None:
                total_uss += float(mem_full_info.uss) / 1.0e6
            total_num_fds += int(stat.get("num_fds", 0))

        tags = {"ip": self._ip, "Component": component_name}
        if pid:
            tags["pid"] = pid

        records = []
        records.append(
            Record(
                gauge=METRICS_GAUGES["component_cpu_percentage"],
                value=total_cpu_percentage,
                tags=tags,
            )
        )
        records.append(
            Record(
                gauge=METRICS_GAUGES["component_mem_shared_bytes"],
                value=total_shm,
                tags=tags,
            )
        )
        records.append(
            Record(
                gauge=METRICS_GAUGES["component_rss_mb"],
                value=total_rss,
                tags=tags,
            )
        )
        if total_uss > 0.0:
            records.append(
                Record(
                    gauge=METRICS_GAUGES["component_uss_mb"],
                    value=total_uss,
                    tags=tags,
                )
            )
        records.append(
            Record(
                gauge=METRICS_GAUGES["component_num_fds"],
                value=total_num_fds,
                tags=tags,
            )
        )

        return records

    def generate_worker_stats_record(self, worker_stats: List[dict]) -> List[Record]:
        """Generate a list of Record class for worker proceses.

        This API automatically sets the component_name of record as
        the name of worker processes. I.e., ray::* so that we can report
        per task/actor (grouped by a func/class name) resource usages.

        Args:
            stats: a list of stats dict generated by `psutil.as_dict`
                for worker processes.
        """
        # worekr cmd name (ray::*) -> stats dict.
        proc_name_to_stats = defaultdict(list)
        for stat in worker_stats:
            cmdline = stat.get("cmdline")
            # All ray processes start with ray::
            if cmdline and len(cmdline) > 0 and cmdline[0].startswith("ray::"):
                proc_name = cmdline[0]
                proc_name_to_stats[proc_name].append(stat)
            # We will lose worker stats that don't follow the ray worker proc
            # naming convention. Theoretically, there should be no data loss here
            # because all worker processes are renamed to ray::.

        records = []
        for proc_name, stats in proc_name_to_stats.items():
            records.extend(self._generate_system_stats_record(stats, proc_name))

        # Reset worker metrics that are from finished processes.
        new_proc_names = set(proc_name_to_stats.keys())
        stale_procs = self._latest_worker_proc_names - new_proc_names
        self._latest_worker_proc_names = new_proc_names

        for stale_proc_name in stale_procs:
            records.extend(self._generate_reseted_stats_record(stale_proc_name))

        return records

    def _to_records(self, stats, cluster_stats) -> List[Record]:
        records_reported = []
        ip = stats["ip"]
        is_head_node = str(self._is_head_node).lower()

        # Common tags for node-level metrics
        node_tags = {"ip": ip, "IsHeadNode": is_head_node}

        # -- Instance count of cluster --
        # Only report cluster stats on head node
        if "autoscaler_report" in cluster_stats and self._is_head_node:
            active_nodes = cluster_stats["autoscaler_report"]["active_nodes"]
            for node_type, active_node_count in active_nodes.items():
                records_reported.append(
                    Record(
                        gauge=METRICS_GAUGES["cluster_active_nodes"],
                        value=active_node_count,
                        tags={"node_type": node_type},
                    )
                )

            failed_nodes = cluster_stats["autoscaler_report"]["failed_nodes"]
            failed_nodes_dict = {}
            for node_ip, node_type in failed_nodes:
                if node_type in failed_nodes_dict:
                    failed_nodes_dict[node_type] += 1
                else:
                    failed_nodes_dict[node_type] = 1

            for node_type, failed_node_count in failed_nodes_dict.items():
                records_reported.append(
                    Record(
                        gauge=METRICS_GAUGES["cluster_failed_nodes"],
                        value=failed_node_count,
                        tags={"node_type": node_type},
                    )
                )

            pending_nodes = cluster_stats["autoscaler_report"]["pending_nodes"]
            pending_nodes_dict = {}
            for node_ip, node_type, status_message in pending_nodes:
                if node_type in pending_nodes_dict:
                    pending_nodes_dict[node_type] += 1
                else:
                    pending_nodes_dict[node_type] = 1

            for node_type, pending_node_count in pending_nodes_dict.items():
                records_reported.append(
                    Record(
                        gauge=METRICS_GAUGES["cluster_pending_nodes"],
                        value=pending_node_count,
                        tags={"node_type": node_type},
                    )
                )

        # -- CPU per node --
        cpu_usage = float(stats["cpu"])
        cpu_record = Record(
            gauge=METRICS_GAUGES["node_cpu_utilization"],
            value=cpu_usage,
            tags=node_tags,
        )

        cpu_count, _ = stats["cpus"]
        cpu_count_record = Record(
            gauge=METRICS_GAUGES["node_cpu_count"], value=cpu_count, tags=node_tags
        )

        # -- Mem per node --
        mem_total, mem_available, _, mem_used = stats["mem"]
        mem_used_record = Record(
            gauge=METRICS_GAUGES["node_mem_used"], value=mem_used, tags=node_tags
        )
        mem_available_record = Record(
            gauge=METRICS_GAUGES["node_mem_available"],
            value=mem_available,
            tags=node_tags,
        )
        mem_total_record = Record(
            gauge=METRICS_GAUGES["node_mem_total"], value=mem_total, tags=node_tags
        )

        shm_used = stats["shm"]
        if shm_used:
            node_mem_shared = Record(
                gauge=METRICS_GAUGES["node_mem_shared_bytes"],
                value=shm_used,
                tags=node_tags,
            )
            records_reported.append(node_mem_shared)

        # The output example of GpuUtilizationInfo.
        """
        {'index': 0,
        'uuid': 'GPU-36e1567d-37ed-051e-f8ff-df807517b396',
        'name': 'NVIDIA A10G',
        'utilization_gpu': 1,
        'memory_used': 0,
        'memory_total': 22731}
        """
        # -- GPU per node --
        gpus = stats["gpus"]
        gpus_available = len(gpus)

        if gpus_available:
            for gpu in gpus:
                gpus_utilization, gram_used, gram_total = 0, 0, 0
                # Consume GPU may not report its utilization.
                if gpu["utilization_gpu"] is not None:
                    gpus_utilization += gpu["utilization_gpu"]
                gram_used += gpu["memory_used"]
                gram_total += gpu["memory_total"]
                gpu_index = gpu.get("index")
                gpu_name = gpu.get("name")

                gram_available = gram_total - gram_used

                if gpu_index is not None:
                    gpu_tags = {**node_tags, "GpuIndex": str(gpu_index)}
                    if gpu_name:
                        gpu_tags["GpuDeviceName"] = gpu_name

                    # There's only 1 GPU per each index, so we record 1 here.
                    gpus_available_record = Record(
                        gauge=METRICS_GAUGES["node_gpus_available"],
                        value=1,
                        tags=gpu_tags,
                    )
                    gpus_utilization_record = Record(
                        gauge=METRICS_GAUGES["node_gpus_utilization"],
                        value=gpus_utilization,
                        tags=gpu_tags,
                    )
                    gram_used_record = Record(
                        gauge=METRICS_GAUGES["node_gram_used"],
                        value=gram_used,
                        tags=gpu_tags,
                    )
                    gram_available_record = Record(
                        gauge=METRICS_GAUGES["node_gram_available"],
                        value=gram_available,
                        tags=gpu_tags,
                    )
                    records_reported.extend(
                        [
                            gpus_available_record,
                            gpus_utilization_record,
                            gram_used_record,
                            gram_available_record,
                        ]
                    )

        # -- TPU per node --
        tpus = stats["tpus"]

        for tpu in tpus:
            tpu_index = tpu.get("index")
            tpu_name = tpu.get("name")
            tpu_type = tpu.get("tpu_type")
            tpu_topology = tpu.get("tpu_topology")
            tensorcore_utilization = tpu.get("tensorcore_utilization")
            hbm_utilization = tpu.get("hbm_utilization")
            duty_cycle = tpu.get("duty_cycle")
            memory_used = tpu.get("memory_used")
            memory_total = tpu.get("memory_total")

            tpu_tags = {
                **node_tags,
                "TpuIndex": str(tpu_index),
                "TpuDeviceName": tpu_name,
                "TpuType": tpu_type,
                "TpuTopology": tpu_topology,
            }
            tensorcore_utilization_record = Record(
                gauge=METRICS_GAUGES["tpu_tensorcore_utilization"],
                value=tensorcore_utilization,
                tags=tpu_tags,
            )
            hbm_utilization_record = Record(
                gauge=METRICS_GAUGES["tpu_memory_bandwidth_utilization"],
                value=hbm_utilization,
                tags=tpu_tags,
            )
            duty_cycle_record = Record(
                gauge=METRICS_GAUGES["tpu_duty_cycle"],
                value=duty_cycle,
                tags=tpu_tags,
            )
            memory_used_record = Record(
                gauge=METRICS_GAUGES["tpu_memory_used"],
                value=memory_used,
                tags=tpu_tags,
            )
            memory_total_record = Record(
                gauge=METRICS_GAUGES["tpu_memory_total"],
                value=memory_total,
                tags=tpu_tags,
            )
            records_reported.extend(
                [
                    tensorcore_utilization_record,
                    hbm_utilization_record,
                    duty_cycle_record,
                    memory_used_record,
                    memory_total_record,
                ]
            )

        # -- Disk per node --
        disk_io_stats = stats["disk_io"]
        disk_read_record = Record(
            gauge=METRICS_GAUGES["node_disk_io_read"],
            value=disk_io_stats[0],
            tags=node_tags,
        )
        disk_write_record = Record(
            gauge=METRICS_GAUGES["node_disk_io_write"],
            value=disk_io_stats[1],
            tags=node_tags,
        )
        disk_read_count_record = Record(
            gauge=METRICS_GAUGES["node_disk_io_read_count"],
            value=disk_io_stats[2],
            tags=node_tags,
        )
        disk_write_count_record = Record(
            gauge=METRICS_GAUGES["node_disk_io_write_count"],
            value=disk_io_stats[3],
            tags=node_tags,
        )
        disk_io_speed_stats = stats["disk_io_speed"]
        disk_read_speed_record = Record(
            gauge=METRICS_GAUGES["node_disk_io_read_speed"],
            value=disk_io_speed_stats[0],
            tags=node_tags,
        )
        disk_write_speed_record = Record(
            gauge=METRICS_GAUGES["node_disk_io_write_speed"],
            value=disk_io_speed_stats[1],
            tags=node_tags,
        )
        disk_read_iops_record = Record(
            gauge=METRICS_GAUGES["node_disk_read_iops"],
            value=disk_io_speed_stats[2],
            tags=node_tags,
        )
        disk_write_iops_record = Record(
            gauge=METRICS_GAUGES["node_disk_write_iops"],
            value=disk_io_speed_stats[3],
            tags=node_tags,
        )
        used = stats["disk"]["/"].used
        free = stats["disk"]["/"].free
        disk_utilization = float(used / (used + free)) * 100
        disk_usage_record = Record(
            gauge=METRICS_GAUGES["node_disk_usage"], value=used, tags=node_tags
        )
        disk_free_record = Record(
            gauge=METRICS_GAUGES["node_disk_free"], value=free, tags=node_tags
        )
        disk_utilization_percentage_record = Record(
            gauge=METRICS_GAUGES["node_disk_utilization_percentage"],
            value=disk_utilization,
            tags=node_tags,
        )

        # -- Network speed (send/receive) stats per node --
        network_stats = stats["network"]
        network_sent_record = Record(
            gauge=METRICS_GAUGES["node_network_sent"],
            value=network_stats[0],
            tags=node_tags,
        )
        network_received_record = Record(
            gauge=METRICS_GAUGES["node_network_received"],
            value=network_stats[1],
            tags=node_tags,
        )

        # -- Network speed (send/receive) per node --
        network_speed_stats = stats["network_speed"]
        network_send_speed_record = Record(
            gauge=METRICS_GAUGES["node_network_send_speed"],
            value=network_speed_stats[0],
            tags=node_tags,
        )
        network_receive_speed_record = Record(
            gauge=METRICS_GAUGES["node_network_receive_speed"],
            value=network_speed_stats[1],
            tags=node_tags,
        )

        """
        Record system stats.
        """

        if self._is_head_node:
            gcs_stats = stats["gcs"]
            if gcs_stats:
                records_reported.extend(
                    self._generate_system_stats_record(
                        [gcs_stats], "gcs", pid=str(gcs_stats["pid"])
                    )
                )

        # Record component metrics.
        raylet_stats = stats["raylet"]
        if raylet_stats:
            raylet_pid = str(raylet_stats["pid"])
            records_reported.extend(
                self._generate_system_stats_record(
                    [raylet_stats], "raylet", pid=raylet_pid
                )
            )
        workers_stats = stats["workers"]
        records_reported.extend(self.generate_worker_stats_record(workers_stats))
        agent_stats = stats["agent"]
        if agent_stats:
            agent_pid = str(agent_stats["pid"])
            records_reported.extend(
                self._generate_system_stats_record(
                    [agent_stats], "agent", pid=agent_pid
                )
            )

        # NOTE: Dashboard metrics is recorded within the dashboard because
        # it can be deployed as a standalone instance. It shouldn't
        # depend on the agent.

        records_reported.extend(
            [
                cpu_record,
                cpu_count_record,
                mem_used_record,
                mem_available_record,
                mem_total_record,
                disk_read_record,
                disk_write_record,
                disk_read_count_record,
                disk_write_count_record,
                disk_read_speed_record,
                disk_write_speed_record,
                disk_read_iops_record,
                disk_write_iops_record,
                disk_usage_record,
                disk_free_record,
                disk_utilization_percentage_record,
                network_sent_record,
                network_received_record,
                network_send_speed_record,
                network_receive_speed_record,
            ]
        )

        return records_reported

    async def _run_loop(self):
        """Get any changes to the log files and push updates to kv."""
        loop = get_or_create_event_loop()

        while True:
            try:
                # Fetch autoscaler debug status
                autoscaler_status_json_bytes: Optional[bytes] = None
                if self._is_head_node:
                    autoscaler_status_json_bytes = (
                        await self._gcs_client.async_internal_kv_get(
                            DEBUG_AUTOSCALING_STATUS.encode(),
                            None,
                            timeout=GCS_RPC_TIMEOUT_SECONDS,
                        )
                    )
                    self._gcs_pid = await self._gcs_client.async_internal_kv_get(
                        GCS_PID_KEY.encode(),
                        None,
                        timeout=GCS_RPC_TIMEOUT_SECONDS,
                    )
                    self._gcs_pid = (
                        int(self._gcs_pid.decode()) if self._gcs_pid else None
                    )

                # NOTE: Stats collection is executed inside the thread-pool
                #       executor (TPE) to avoid blocking the Agent's event-loop
                json_payload = await loop.run_in_executor(
                    self._executor,
                    self._compose_stats_payload,
                    autoscaler_status_json_bytes,
                )

                await self._gcs_client.async_publish_node_resource_usage(
                    self._key, json_payload
                )

            except Exception:
                logger.exception("Error publishing node physical stats.")

            await asyncio.sleep(reporter_consts.REPORTER_UPDATE_INTERVAL_MS / 1000)

    def _compose_stats_payload(
        self, cluster_autoscaling_stats_json: Optional[bytes]
    ) -> str:
        stats = self._collect_stats()

        # Report stats only when metrics collection is enabled.
        if not self._metrics_collection_disabled:
            cluster_stats = (
                json.loads(cluster_autoscaling_stats_json.decode())
                if cluster_autoscaling_stats_json
                else {}
            )

            records = self._to_records(stats, cluster_stats)

            if RAY_EXPERIMENTAL_ENABLE_OPEN_TELEMETRY_ON_AGENT:
                self._open_telemetry_metric_recorder.record_and_export(
                    records,
                    global_tags={
                        "Version": ray.__version__,
                        "SessionName": self._session_name,
                    },
                )
            else:
                self._metrics_agent.record_and_export(
                    records,
                    global_tags={
                        "Version": ray.__version__,
                        "SessionName": self._session_name,
                    },
                )

            self._metrics_agent.clean_all_dead_worker_metrics()

        return jsonify_asdict(stats)

    async def run(self, server):
        if server:
            reporter_pb2_grpc.add_ReporterServiceServicer_to_server(self, server)
            if RAY_EXPERIMENTAL_ENABLE_OPEN_TELEMETRY_ON_CORE:
                metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(
                    self, server
                )

        await self._run_loop()

    @staticmethod
    def is_minimal_module():
        return False

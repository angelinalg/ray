// Copyright 2017 The Ray Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//  http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include "ray/core_worker/transport/actor_task_submitter.h"

#include <deque>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include "ray/gcs/pb_util.h"

namespace ray {
namespace core {

void ActorTaskSubmitter::NotifyGCSWhenActorOutOfScope(
    const ActorID &actor_id, uint64_t num_restarts_due_to_lineage_reconstruction) {
  const auto actor_creation_return_id = ObjectID::ForActorHandle(actor_id);
  auto actor_out_of_scope_callback = [this,
                                      actor_id,
                                      num_restarts_due_to_lineage_reconstruction](
                                         const ObjectID &object_id) {
    {
      absl::MutexLock lock(&mu_);
      if (auto iter = client_queues_.find(actor_id); iter != client_queues_.end()) {
        if (iter->second.state != rpc::ActorTableData::DEAD) {
          iter->second.pending_out_of_scope_death = true;
        }
      }
    }
    actor_creator_.AsyncReportActorOutOfScope(
        actor_id, num_restarts_due_to_lineage_reconstruction, [actor_id](Status status) {
          if (!status.ok()) {
            RAY_LOG(ERROR).WithField(actor_id)
                << "Failed to report actor out of scope: " << status
                << ". The actor will not be killed";
          }
        });
  };

  if (!reference_counter_->AddObjectOutOfScopeOrFreedCallback(
          actor_creation_return_id,
          [actor_out_of_scope_callback](const ObjectID &object_id) {
            actor_out_of_scope_callback(object_id);
          })) {
    RAY_LOG(DEBUG).WithField(actor_id) << "Actor already out of scope";
    actor_out_of_scope_callback(actor_creation_return_id);
  }
}

void ActorTaskSubmitter::AddActorQueueIfNotExists(const ActorID &actor_id,
                                                  int32_t max_pending_calls,
                                                  bool execute_out_of_order,
                                                  bool fail_if_actor_unreachable,
                                                  bool owned) {
  bool inserted;
  {
    absl::MutexLock lock(&mu_);
    // No need to check whether the insert was successful, since it is possible
    // for this worker to have multiple references to the same actor.
    RAY_LOG(INFO).WithField(actor_id)
        << "Set actor max pending calls to " << max_pending_calls;
    inserted = client_queues_
                   .emplace(actor_id,
                            ClientQueue(actor_id,
                                        execute_out_of_order,
                                        max_pending_calls,
                                        fail_if_actor_unreachable,
                                        owned))
                   .second;
  }
  if (owned && inserted) {
    // Actor owner is responsible for notifying GCS when the
    // actor is out of scope so that GCS can kill the actor.
    NotifyGCSWhenActorOutOfScope(actor_id,
                                 /*num_restarts_due_to_lineage_reconstruction*/ 0);
  }
}

Status ActorTaskSubmitter::SubmitActorCreationTask(TaskSpecification task_spec) {
  RAY_CHECK(task_spec.IsActorCreationTask());
  const auto actor_id = task_spec.ActorCreationId();
  const auto task_id = task_spec.TaskId();
  RAY_LOG(DEBUG).WithField(actor_id).WithField(task_id)
      << "Submitting actor creation task";
  resolver_.ResolveDependencies(task_spec, [this, task_spec](Status status) mutable {
    // NOTE: task_spec here is capture copied (from a stack variable) and also
    // mutable. (Mutations to the variable are expected to be shared inside and
    // outside of this closure).
    const auto actor_id = task_spec.ActorCreationId();
    const auto task_id = task_spec.TaskId();
    task_manager_.MarkDependenciesResolved(task_id);
    if (!status.ok()) {
      RAY_LOG(WARNING).WithField(actor_id).WithField(task_id)
          << "Resolving actor creation task dependencies failed " << status;
      RAY_UNUSED(task_manager_.FailOrRetryPendingTask(
          task_id, rpc::ErrorType::DEPENDENCY_RESOLUTION_FAILED, &status));
      return;
    }
    RAY_LOG(DEBUG).WithField(actor_id).WithField(task_id)
        << "Actor creation task dependencies resolved";
    // The actor creation task will be sent to
    // gcs server directly after the in-memory dependent objects are resolved. For
    // more details please see the protocol of actor management based on gcs.
    // https://docs.google.com/document/d/1EAWide-jy05akJp6OMtDn58XOK7bUyruWMia4E-fV28/edit?usp=sharing
    RAY_LOG(DEBUG).WithField(actor_id).WithField(task_id) << "Creating actor via GCS";
    actor_creator_.AsyncCreateActor(
        task_spec,
        [this, actor_id, task_id](Status status, const rpc::CreateActorReply &reply) {
          if (status.ok() || status.IsCreationTaskError()) {
            rpc::PushTaskReply push_task_reply;
            push_task_reply.mutable_borrowed_refs()->CopyFrom(reply.borrowed_refs());
            if (status.IsCreationTaskError()) {
              RAY_LOG(INFO).WithField(actor_id).WithField(task_id)
                  << "Actor creation failed and we will not be retrying the "
                     "creation task";
              // Update the task execution error to be CreationTaskError.
              push_task_reply.set_task_execution_error(status.ToString());
            } else {
              RAY_LOG(DEBUG).WithField(actor_id).WithField(task_id) << "Created actor";
            }
            // NOTE: When actor creation task failed we will not retry the creation
            // task so just marking the task fails.
            task_manager_.CompletePendingTask(
                task_id,
                push_task_reply,
                reply.actor_address(),
                /*is_application_error=*/status.IsCreationTaskError());
          } else {
            // Either fails the rpc call or actor scheduling cancelled.
            rpc::RayErrorInfo ray_error_info;
            if (status.IsSchedulingCancelled()) {
              RAY_LOG(DEBUG).WithField(actor_id).WithField(task_id)
                  << "Actor creation cancelled";
              task_manager_.MarkTaskCanceled(task_id);
              if (reply.has_death_cause()) {
                ray_error_info.mutable_actor_died_error()->CopyFrom(reply.death_cause());
              }
            } else {
              RAY_LOG(INFO).WithField(actor_id).WithField(task_id)
                  << "Failed to create actor with status: " << status;
            }
            // Actor creation task retry happens in GCS
            // and transient rpc errors are retried in gcs client
            // so we don't need to retry here.
            RAY_UNUSED(task_manager_.FailPendingTask(
                task_id,
                rpc::ErrorType::ACTOR_CREATION_FAILED,
                &status,
                ray_error_info.has_actor_died_error() ? &ray_error_info : nullptr));
          }
        });
  });

  return Status::OK();
}

Status ActorTaskSubmitter::SubmitTask(TaskSpecification task_spec) {
  auto task_id = task_spec.TaskId();
  auto actor_id = task_spec.ActorId();
  RAY_LOG(DEBUG).WithField(task_id) << "Submitting task";
  RAY_CHECK(task_spec.IsActorTask());

  bool task_queued = false;
  uint64_t send_pos = 0;
  {
    absl::MutexLock lock(&mu_);
    auto queue = client_queues_.find(actor_id);
    RAY_CHECK(queue != client_queues_.end());
    if (queue->second.state == rpc::ActorTableData::DEAD &&
        queue->second.is_restartable && queue->second.owned) {
      RestartActorForLineageReconstruction(actor_id);
    }
    if (queue->second.state != rpc::ActorTableData::DEAD) {
      // We must fix the send order prior to resolving dependencies, which may
      // complete out of order. This ensures that we will not deadlock due to
      // backpressure. The receiving actor will execute the tasks according to
      // this sequence number.
      send_pos = task_spec.SequenceNumber();
      queue->second.actor_submit_queue->Emplace(send_pos, task_spec);
      queue->second.cur_pending_calls++;
      task_queued = true;
    }
  }

  if (task_queued) {
    io_service_.post(
        [task_spec, send_pos, this]() mutable {
          // We must release the lock before resolving the task dependencies since
          // the callback may get called in the same call stack.
          auto actor_id = task_spec.ActorId();
          auto task_id = task_spec.TaskId();
          resolver_.ResolveDependencies(
              task_spec, [this, send_pos, actor_id, task_id](Status status) {
                task_manager_.MarkDependenciesResolved(task_id);
                bool fail_or_retry_task = false;
                {
                  absl::MutexLock lock(&mu_);
                  auto queue = client_queues_.find(actor_id);
                  RAY_CHECK(queue != client_queues_.end());
                  auto &actor_submit_queue = queue->second.actor_submit_queue;
                  // Only dispatch tasks if the submitted task is still queued. The task
                  // may have been dequeued if the actor has since failed.
                  if (actor_submit_queue->Contains(send_pos)) {
                    if (status.ok()) {
                      actor_submit_queue->MarkDependencyResolved(send_pos);
                      SendPendingTasks(actor_id);
                    } else {
                      fail_or_retry_task = true;
                      actor_submit_queue->MarkDependencyFailed(send_pos);
                    }
                  }
                }

                if (fail_or_retry_task) {
                  GetTaskManagerWithoutMu().FailOrRetryPendingTask(
                      task_id, rpc::ErrorType::DEPENDENCY_RESOLUTION_FAILED, &status);
                }
              });
        },
        "ActorTaskSubmitter::SubmitTask");
  } else {
    // Do not hold the lock while calling into task_manager_.
    task_manager_.MarkTaskCanceled(task_id);
    rpc::ErrorType error_type;
    rpc::RayErrorInfo error_info;
    {
      absl::MutexLock lock(&mu_);
      const auto queue_it = client_queues_.find(task_spec.ActorId());
      const auto &death_cause = queue_it->second.death_cause;
      error_info = gcs::GetErrorInfoFromActorDeathCause(death_cause);
      error_type = error_info.error_type();
    }
    auto status = Status::IOError("cancelling task of dead actor");
    // No need to increment the number of completed tasks since the actor is
    // dead.
    bool fail_immediately =
        error_info.has_actor_died_error() &&
        error_info.actor_died_error().has_oom_context() &&
        error_info.actor_died_error().oom_context().fail_immediately();
    GetTaskManagerWithoutMu().FailOrRetryPendingTask(task_id,
                                                     error_type,
                                                     &status,
                                                     &error_info,
                                                     /*mark_task_object_failed*/ true,
                                                     fail_immediately);
  }

  // If the task submission subsequently fails, then the client will receive
  // the error in a callback.
  return Status::OK();
}

void ActorTaskSubmitter::DisconnectRpcClient(ClientQueue &queue) {
  queue.rpc_client = nullptr;
  core_worker_client_pool_.Disconnect(WorkerID::FromBinary(queue.worker_id));
  queue.worker_id.clear();
}

void ActorTaskSubmitter::FailInflightTasksOnRestart(
    const absl::flat_hash_map<TaskAttempt, rpc::ClientCallback<rpc::PushTaskReply>>
        &inflight_task_callbacks) {
  // NOTE(kfstorm): We invoke the callbacks with a bad status to act like there's a
  // network issue. We don't call `task_manager_.FailOrRetryPendingTask` directly because
  // there's much more work to do in the callback.
  auto status = Status::IOError("The actor was restarted");
  for (const auto &[_, callback] : inflight_task_callbacks) {
    callback(status, rpc::PushTaskReply());
  }
}

void ActorTaskSubmitter::ConnectActor(const ActorID &actor_id,
                                      const rpc::Address &address,
                                      int64_t num_restarts) {
  RAY_LOG(DEBUG).WithField(actor_id).WithField(WorkerID::FromBinary(address.worker_id()))
      << "Connecting to actor";

  absl::flat_hash_map<TaskAttempt, rpc::ClientCallback<rpc::PushTaskReply>>
      inflight_task_callbacks;

  {
    absl::MutexLock lock(&mu_);

    auto queue = client_queues_.find(actor_id);
    RAY_CHECK(queue != client_queues_.end());
    if (num_restarts < queue->second.num_restarts) {
      // This message is about an old version of the actor and the actor has
      // already restarted since then. Skip the connection.
      RAY_LOG(INFO).WithField(actor_id)
          << "Skip actor connection that has already been restarted";
      return;
    }

    if (queue->second.rpc_client &&
        queue->second.rpc_client->Addr().ip_address() == address.ip_address() &&
        queue->second.rpc_client->Addr().port() == address.port()) {
      RAY_LOG(DEBUG).WithField(actor_id) << "Skip actor that has already been connected";
      return;
    }

    if (queue->second.state == rpc::ActorTableData::DEAD) {
      // This message is about an old version of the actor and the actor has
      // already died since then. Skip the connection.
      return;
    }

    queue->second.num_restarts = num_restarts;
    if (queue->second.rpc_client) {
      // Clear the client to the old version of the actor.
      DisconnectRpcClient(queue->second);
      inflight_task_callbacks = std::move(queue->second.inflight_task_callbacks);
      queue->second.inflight_task_callbacks.clear();
    }

    queue->second.state = rpc::ActorTableData::ALIVE;
    // Update the mapping so new RPCs go out with the right intended worker id.
    queue->second.worker_id = address.worker_id();
    // Create a new connection to the actor.
    queue->second.rpc_client = core_worker_client_pool_.GetOrConnect(address);

    SendPendingTasks(actor_id);
  }

  // NOTE(kfstorm): We need to make sure the lock is released before invoking callbacks.
  FailInflightTasksOnRestart(inflight_task_callbacks);
}

void ActorTaskSubmitter::RestartActorForLineageReconstruction(const ActorID &actor_id) {
  RAY_LOG(INFO).WithField(actor_id) << "Reconstructing actor";
  auto queue = client_queues_.find(actor_id);
  RAY_CHECK(queue != client_queues_.end());
  RAY_CHECK(queue->second.owned) << "Only owner can restart the dead actor";
  RAY_CHECK(queue->second.is_restartable) << "This actor is no longer restartable";
  queue->second.state = rpc::ActorTableData::RESTARTING;
  queue->second.num_restarts_due_to_lineage_reconstructions += 1;
  actor_creator_.AsyncRestartActorForLineageReconstruction(
      actor_id,
      queue->second.num_restarts_due_to_lineage_reconstructions,
      [this,
       actor_id,
       num_restarts_due_to_lineage_reconstructions =
           queue->second.num_restarts_due_to_lineage_reconstructions](Status status) {
        if (!status.ok()) {
          RAY_LOG(ERROR).WithField(actor_id)
              << "Failed to reconstruct actor. Error message: " << status.ToString();
        } else {
          // Notify GCS when the actor is out of scope again.
          NotifyGCSWhenActorOutOfScope(actor_id,
                                       num_restarts_due_to_lineage_reconstructions);
        }
      });
}

void ActorTaskSubmitter::DisconnectActor(const ActorID &actor_id,
                                         int64_t num_restarts,
                                         bool dead,
                                         const rpc::ActorDeathCause &death_cause,
                                         bool is_restartable) {
  RAY_LOG(DEBUG).WithField(actor_id) << "Disconnecting from actor, death context type="
                                     << gcs::GetActorDeathCauseString(death_cause);

  absl::flat_hash_map<TaskAttempt, rpc::ClientCallback<rpc::PushTaskReply>>
      inflight_task_callbacks;
  std::deque<std::shared_ptr<PendingTaskWaitingForDeathInfo>> wait_for_death_info_tasks;
  std::vector<TaskID> task_ids_to_fail;
  {
    absl::MutexLock lock(&mu_);
    auto queue = client_queues_.find(actor_id);
    RAY_CHECK(queue != client_queues_.end());
    if (!dead) {
      RAY_CHECK_GT(num_restarts, 0);
    }
    if (num_restarts <= queue->second.num_restarts && !dead) {
      // This message is about an old version of the actor that has already been
      // restarted successfully. Skip the message handling.
      RAY_LOG(INFO).WithField(actor_id)
          << "Skip actor disconnection that has already been restarted";
      return;
    }

    // The actor failed, so erase the client for now. Either the actor is
    // permanently dead or the new client will be inserted once the actor is
    // restarted.
    DisconnectRpcClient(queue->second);
    inflight_task_callbacks = std::move(queue->second.inflight_task_callbacks);
    queue->second.inflight_task_callbacks.clear();

    if (dead) {
      queue->second.state = rpc::ActorTableData::DEAD;
      queue->second.death_cause = death_cause;
      queue->second.pending_out_of_scope_death = false;
      queue->second.is_restartable = is_restartable;

      if (queue->second.is_restartable && queue->second.owned) {
        // Actor is out of scope so there should be no inflight actor tasks.
        RAY_CHECK(queue->second.wait_for_death_info_tasks.empty());
        RAY_CHECK(inflight_task_callbacks.empty());
        if (!queue->second.actor_submit_queue->Empty()) {
          // There are pending lineage reconstruction tasks.
          RestartActorForLineageReconstruction(actor_id);
        }
      } else {
        // If there are pending requests, treat the pending tasks as failed.
        RAY_LOG(INFO).WithField(actor_id)
            << "Failing pending tasks for actor because the actor is already dead.";

        task_ids_to_fail = queue->second.actor_submit_queue->ClearAllTasks();
        // We need to execute this outside of the lock to prevent deadlock.
        wait_for_death_info_tasks = std::move(queue->second.wait_for_death_info_tasks);
        // Reset the queue
        queue->second.wait_for_death_info_tasks =
            std::deque<std::shared_ptr<PendingTaskWaitingForDeathInfo>>();
      }
    } else if (queue->second.state != rpc::ActorTableData::DEAD) {
      // Only update the actor's state if it is not permanently dead. The actor
      // will eventually get restarted or marked as permanently dead.
      queue->second.state = rpc::ActorTableData::RESTARTING;
      queue->second.num_restarts = num_restarts;
    }
  }

  if (task_ids_to_fail.size() + wait_for_death_info_tasks.size() != 0) {
    // Failing tasks has to be done without mu_ hold because the callback
    // might require holding mu_ which will lead to a deadlock.
    auto status = Status::IOError("cancelling all pending tasks of dead actor");
    const auto error_info = gcs::GetErrorInfoFromActorDeathCause(death_cause);
    const auto error_type = error_info.error_type();

    for (auto &task_id : task_ids_to_fail) {
      // No need to increment the number of completed tasks since the actor is
      // dead.
      task_manager_.MarkTaskCanceled(task_id);
      // This task may have been waiting for dependency resolution, so cancel
      // this first.
      resolver_.CancelDependencyResolution(task_id);
      bool fail_immediatedly =
          error_info.has_actor_died_error() &&
          error_info.actor_died_error().has_oom_context() &&
          error_info.actor_died_error().oom_context().fail_immediately();
      GetTaskManagerWithoutMu().FailOrRetryPendingTask(task_id,
                                                       error_type,
                                                       &status,
                                                       &error_info,
                                                       /*mark_task_object_failed*/ true,
                                                       fail_immediatedly);
    }
    if (!wait_for_death_info_tasks.empty()) {
      RAY_LOG(DEBUG).WithField(actor_id) << "Failing tasks waiting for death info, size="
                                         << wait_for_death_info_tasks.size();
      for (auto &task : wait_for_death_info_tasks) {
        GetTaskManagerWithoutMu().FailPendingTask(
            task->task_spec.TaskId(), error_type, &task->status, &error_info);
      }
    }
  }
  // NOTE(kfstorm): We need to make sure the lock is released before invoking callbacks.
  FailInflightTasksOnRestart(inflight_task_callbacks);
}

void ActorTaskSubmitter::FailTaskWithError(const PendingTaskWaitingForDeathInfo &task) {
  rpc::RayErrorInfo error_info;
  if (!task.actor_preempted) {
    error_info = task.timeout_error_info;
  } else {
    // Special error for preempted actor. The task "timed out" because the actor may
    // not have sent a notification to the gcs; regardless we already know it's
    // preempted and it's dead.
    auto actor_death_cause = error_info.mutable_actor_died_error();
    auto actor_died_error_context = actor_death_cause->mutable_actor_died_error_context();
    actor_died_error_context->set_reason(rpc::ActorDiedErrorContext::NODE_DIED);
    actor_died_error_context->set_actor_id(task.task_spec.ActorId().Binary());
    auto node_death_info = actor_died_error_context->mutable_node_death_info();
    node_death_info->set_reason(rpc::NodeDeathInfo::AUTOSCALER_DRAIN_PREEMPTED);
    node_death_info->set_reason_message(
        "the node was inferred to be dead due to draining.");
    error_info.set_error_type(rpc::ErrorType::ACTOR_DIED);
    error_info.set_error_message("Actor died by preemption.");
  }
  GetTaskManagerWithoutMu().FailPendingTask(
      task.task_spec.TaskId(), error_info.error_type(), &task.status, &error_info);
}

void ActorTaskSubmitter::CheckTimeoutTasks() {
  // For each task in `wait_for_death_info_tasks`, if it times out, fail it with
  // timeout_error_info. But operating on the queue requires the mu_ lock; while calling
  // FailPendingTask requires the opposite. So we copy the tasks out from the queue within
  // the lock. This requires putting the data into shared_ptr.
  std::vector<std::shared_ptr<PendingTaskWaitingForDeathInfo>> timeout_tasks;
  int64_t now = current_time_ms();
  {
    absl::MutexLock lock(&mu_);
    for (auto &[actor_id, client_queue] : client_queues_) {
      auto &deque = client_queue.wait_for_death_info_tasks;
      auto deque_itr = deque.begin();
      while (deque_itr != deque.end() && (*deque_itr)->deadline_ms < now) {
        // Populate the info of whether the actor is preempted. If so we hard fail the
        // task.
        (*deque_itr)->actor_preempted = client_queue.preempted;
        timeout_tasks.push_back(*deque_itr);
        deque_itr = deque.erase(deque_itr);
      }
    }
  }
  // Note: mu_ released.
  for (auto &task : timeout_tasks) {
    FailTaskWithError(*task);
  }
}

void ActorTaskSubmitter::SendPendingTasks(const ActorID &actor_id) {
  auto it = client_queues_.find(actor_id);
  RAY_CHECK(it != client_queues_.end());
  auto &client_queue = it->second;
  auto &actor_submit_queue = client_queue.actor_submit_queue;
  if (client_queue.pending_out_of_scope_death) {
    // Wait until the actor is dead and then decide
    // whether we should fail pending tasks or restart the actor.
    // If the actor is restarted, ConnectActor will be called
    // and pending tasks will be sent at that time.
    return;
  }
  if (!client_queue.rpc_client) {
    if (client_queue.state == rpc::ActorTableData::RESTARTING &&
        client_queue.fail_if_actor_unreachable) {
      // When `fail_if_actor_unreachable` is true, tasks submitted while the actor is in
      // `RESTARTING` state fail immediately.
      while (true) {
        auto task = actor_submit_queue->PopNextTaskToSend();
        if (!task.has_value()) {
          break;
        }

        io_service_.post(
            [this, task_spec = std::move(task.value().first)] {
              rpc::PushTaskReply reply;
              rpc::Address addr;
              HandlePushTaskReply(
                  Status::IOError("The actor is restarting."), reply, addr, task_spec);
            },
            "ActorTaskSubmitter::SendPendingTasks_ForceFail");
      }
    }
    return;
  }

  // Submit all pending actor_submit_queue->
  while (true) {
    auto task = actor_submit_queue->PopNextTaskToSend();
    if (!task.has_value()) {
      break;
    }
    RAY_CHECK(!client_queue.worker_id.empty());
    PushActorTask(client_queue, /*task_spec=*/task->first, /*skip_queue=*/task->second);
  }
}

void ActorTaskSubmitter::PushActorTask(ClientQueue &queue,
                                       const TaskSpecification &task_spec,
                                       bool skip_queue) {
  const auto task_id = task_spec.TaskId();

  auto request = std::make_unique<rpc::PushTaskRequest>();
  // NOTE(swang): CopyFrom is needed because if we use Swap here and the task
  // fails, then the task data will be gone when the TaskManager attempts to
  // access the task.
  request->mutable_task_spec()->CopyFrom(task_spec.GetMessage());

  request->set_intended_worker_id(queue.worker_id);
  request->set_sequence_number(task_spec.SequenceNumber());

  const auto actor_id = task_spec.ActorId();

  const auto num_queued = queue.inflight_task_callbacks.size();
  RAY_LOG(DEBUG).WithField(task_id).WithField(actor_id)
      << "Pushing task to actor, actor id " << actor_id << " seq no "
      << request->sequence_number() << " num queued " << num_queued;
  if (num_queued >= next_queueing_warn_threshold_) {
    // TODO(ekl) add more debug info about the actor name, etc.
    warn_excess_queueing_(actor_id, num_queued);
    next_queueing_warn_threshold_ *= 2;
  }

  rpc::Address addr(queue.rpc_client->Addr());
  rpc::ClientCallback<rpc::PushTaskReply> reply_callback =
      [this, addr, task_spec](const Status &status, const rpc::PushTaskReply &reply) {
        HandlePushTaskReply(status, reply, addr, task_spec);
      };

  const TaskAttempt task_attempt = std::make_pair(task_id, task_spec.AttemptNumber());
  queue.inflight_task_callbacks.emplace(task_attempt, std::move(reply_callback));
  rpc::ClientCallback<rpc::PushTaskReply> wrapped_callback =
      [this, task_attempt, actor_id](const Status &status, rpc::PushTaskReply &&reply) {
        rpc::ClientCallback<rpc::PushTaskReply> reply_callback;
        {
          absl::MutexLock lock(&mu_);
          auto it = client_queues_.find(actor_id);
          RAY_CHECK(it != client_queues_.end());
          auto &queue = it->second;
          auto callback_it = queue.inflight_task_callbacks.find(task_attempt);
          if (callback_it == queue.inflight_task_callbacks.end()) {
            RAY_LOG(DEBUG).WithField(task_attempt.first)
                << "The task has already been marked as failed. Ignore the reply.";
            return;
          }
          reply_callback = std::move(callback_it->second);
          queue.inflight_task_callbacks.erase(callback_it);
        }
        reply_callback(status, std::move(reply));
      };

  task_manager_.MarkTaskWaitingForExecution(task_id,
                                            NodeID::FromBinary(addr.raylet_id()),
                                            WorkerID::FromBinary(addr.worker_id()));
  queue.rpc_client->PushActorTask(
      std::move(request), skip_queue, std::move(wrapped_callback));
}

void ActorTaskSubmitter::HandlePushTaskReply(const Status &status,
                                             const rpc::PushTaskReply &reply,
                                             const rpc::Address &addr,
                                             const TaskSpecification &task_spec) {
  const auto task_id = task_spec.TaskId();
  const auto actor_id = task_spec.ActorId();

  bool resubmit_generator = false;
  {
    absl::MutexLock lock(&mu_);
    // If the generator was queued up for resubmission for object recovery,
    // resubmit as long as we get a valid reply.
    resubmit_generator = generators_to_resubmit_.erase(task_id) > 0 && status.ok();
    if (resubmit_generator) {
      auto queue_pair = client_queues_.find(actor_id);
      RAY_CHECK(queue_pair != client_queues_.end());
      auto &queue = queue_pair->second;
      queue.cur_pending_calls--;
    }
  }
  if (resubmit_generator) {
    GetTaskManagerWithoutMu().MarkGeneratorFailedAndResubmit(task_id);
    return;
  }

  const bool is_retryable_exception = status.ok() && reply.is_retryable_error();
  /// Whether or not we will retry this actor task.
  auto will_retry = false;

  if (status.ok() && !is_retryable_exception) {
    // status.ok() means the worker completed the reply, either succeeded or with a
    // retryable failure (e.g. user exceptions). We complete only on non-retryable case.
    task_manager_.CompletePendingTask(task_id, reply, addr, reply.is_application_error());
  } else if (status.IsSchedulingCancelled()) {
    std::ostringstream stream;
    stream << "The task " << task_id << " is canceled from an actor " << actor_id
           << " before it executes.";
    const auto &msg = stream.str();
    RAY_LOG(DEBUG) << msg;
    rpc::RayErrorInfo error_info;
    error_info.set_error_message(msg);
    error_info.set_error_type(rpc::ErrorType::TASK_CANCELLED);
    GetTaskManagerWithoutMu().FailPendingTask(task_spec.TaskId(),
                                              rpc::ErrorType::TASK_CANCELLED,
                                              /*status*/ nullptr,
                                              &error_info);
  } else {
    bool is_actor_dead = false;
    bool fail_immediately = false;
    rpc::RayErrorInfo error_info;
    if (status.ok()) {
      // retryable user exception.
      RAY_CHECK(is_retryable_exception);
      error_info = gcs::GetRayErrorInfo(rpc::ErrorType::TASK_EXECUTION_EXCEPTION,
                                        reply.task_execution_error());
    } else {
      // push task failed due to network error. For example, actor is dead
      // and no process response for the push task.
      absl::MutexLock lock(&mu_);
      auto queue_pair = client_queues_.find(actor_id);
      RAY_CHECK(queue_pair != client_queues_.end());
      auto &queue = queue_pair->second;

      // If the actor is already dead, immediately mark the task object as failed.
      // Otherwise, start the grace period, waiting for the actor death reason. Before the
      // deadline:
      // - If we got the death reason: mark the object as failed with that reason.
      // - If we did not get the death reason: raise ACTOR_UNAVAILABLE with the status.
      // - If we did not get the death reason, but *the actor is preempted*: raise
      // ACTOR_DIED. See `CheckTimeoutTasks`.
      is_actor_dead = queue.state == rpc::ActorTableData::DEAD;
      if (is_actor_dead) {
        const auto &death_cause = queue.death_cause;
        error_info = gcs::GetErrorInfoFromActorDeathCause(death_cause);
        fail_immediately = error_info.has_actor_died_error() &&
                           error_info.actor_died_error().has_oom_context() &&
                           error_info.actor_died_error().oom_context().fail_immediately();
      } else {
        // The actor may or may not be dead, but the request failed. Consider the failure
        // temporary. May recognize retry, so fail_immediately = false.
        error_info.set_error_message("The actor is temporarily unavailable: " +
                                     status.ToString());
        error_info.set_error_type(rpc::ErrorType::ACTOR_UNAVAILABLE);
        error_info.mutable_actor_unavailable_error()->set_actor_id(actor_id.Binary());
      }
    }

    // This task may have been waiting for dependency resolution, so cancel
    // this first.
    resolver_.CancelDependencyResolution(task_id);

    will_retry = GetTaskManagerWithoutMu().FailOrRetryPendingTask(
        task_id,
        error_info.error_type(),
        &status,
        &error_info,
        /*mark_task_object_failed*/ is_actor_dead,
        fail_immediately);
    if (!is_actor_dead && !will_retry) {
      // Ran out of retries, last failure = either user exception or actor death.
      if (status.ok()) {
        // last failure = user exception, just complete it with failure.
        RAY_CHECK(reply.is_retryable_error());

        GetTaskManagerWithoutMu().CompletePendingTask(
            task_id, reply, addr, reply.is_application_error());

      } else if (RayConfig::instance().timeout_ms_task_wait_for_death_info() != 0) {
        // last failure = Actor death, but we still see the actor "alive" so we optionally
        // wait for a grace period for the death info.

        int64_t death_info_grace_period_ms =
            current_time_ms() +
            RayConfig::instance().timeout_ms_task_wait_for_death_info();
        absl::MutexLock lock(&mu_);
        auto queue_pair = client_queues_.find(actor_id);
        RAY_CHECK(queue_pair != client_queues_.end());
        auto &queue = queue_pair->second;
        queue.wait_for_death_info_tasks.push_back(
            std::make_shared<PendingTaskWaitingForDeathInfo>(
                death_info_grace_period_ms, task_spec, status, error_info));
        RAY_LOG(INFO).WithField(task_spec.TaskId())
            << "PushActorTask failed because of network error, this task "
               "will be stashed away and waiting for Death info from GCS"
               ", wait_queue_size="
            << queue.wait_for_death_info_tasks.size();
      } else {
        // TODO(vitsai): if we don't need death info, just fail the request.
        {
          absl::MutexLock lock(&mu_);
          auto queue_pair = client_queues_.find(actor_id);
          RAY_CHECK(queue_pair != client_queues_.end());
        }
        GetTaskManagerWithoutMu().FailPendingTask(
            task_spec.TaskId(), error_info.error_type(), &status, &error_info);
      }
    }
  }
  {
    absl::MutexLock lock(&mu_);
    auto queue_pair = client_queues_.find(actor_id);
    RAY_CHECK(queue_pair != client_queues_.end());
    auto &queue = queue_pair->second;
    queue.cur_pending_calls--;
  }
}

std::optional<rpc::ActorTableData::ActorState> ActorTaskSubmitter::GetLocalActorState(
    const ActorID &actor_id) const {
  absl::MutexLock lock(&mu_);

  auto iter = client_queues_.find(actor_id);
  if (iter == client_queues_.end()) {
    return std::nullopt;
  } else {
    return iter->second.state;
  }
}

bool ActorTaskSubmitter::IsActorAlive(const ActorID &actor_id) const {
  absl::MutexLock lock(&mu_);

  auto iter = client_queues_.find(actor_id);
  return (iter != client_queues_.end() && iter->second.rpc_client);
}

std::optional<rpc::Address> ActorTaskSubmitter::GetActorAddress(
    const ActorID &actor_id) const {
  absl::MutexLock lock(&mu_);

  auto iter = client_queues_.find(actor_id);
  if (iter == client_queues_.end()) {
    return std::nullopt;
  }

  const auto &rpc_client = iter->second.rpc_client;
  if (rpc_client == nullptr) {
    return std::nullopt;
  }

  return iter->second.rpc_client->Addr();
}

bool ActorTaskSubmitter::PendingTasksFull(const ActorID &actor_id) const {
  absl::MutexLock lock(&mu_);
  auto it = client_queues_.find(actor_id);
  RAY_CHECK(it != client_queues_.end());
  return it->second.max_pending_calls > 0 &&
         it->second.cur_pending_calls >= it->second.max_pending_calls;
}

size_t ActorTaskSubmitter::NumPendingTasks(const ActorID &actor_id) const {
  absl::MutexLock lock(&mu_);
  auto it = client_queues_.find(actor_id);
  RAY_CHECK(it != client_queues_.end());
  return it->second.cur_pending_calls;
}

bool ActorTaskSubmitter::CheckActorExists(const ActorID &actor_id) const {
  absl::MutexLock lock(&mu_);
  return client_queues_.find(actor_id) != client_queues_.end();
}

std::string ActorTaskSubmitter::DebugString(const ActorID &actor_id) const {
  absl::MutexLock lock(&mu_);
  auto it = client_queues_.find(actor_id);
  RAY_CHECK(it != client_queues_.end());
  std::ostringstream stream;
  stream << "Submitter debug string for actor " << actor_id << " "
         << it->second.DebugString();
  return stream.str();
}

void ActorTaskSubmitter::RetryCancelTask(TaskSpecification task_spec,
                                         bool recursive,
                                         int64_t milliseconds) {
  RAY_LOG(DEBUG).WithField(task_spec.TaskId())
      << "Task cancelation will be retried in " << milliseconds << " ms";
  execute_after(
      io_service_,
      [this, task_spec = std::move(task_spec), recursive] {
        RAY_UNUSED(CancelTask(task_spec, recursive));
      },
      std::chrono::milliseconds(milliseconds));
}

Status ActorTaskSubmitter::CancelTask(TaskSpecification task_spec, bool recursive) {
  // We don't support force_kill = true for actor tasks.
  bool force_kill = false;
  RAY_LOG(INFO).WithField(task_spec.TaskId()).WithField(task_spec.ActorId())
      << "Cancelling an actor task: force_kill: " << force_kill
      << " recursive: " << recursive;

  // Tasks are in one of the following states.
  // - dependencies not resolved
  // - queued
  // - sent
  // - finished.

  const auto actor_id = task_spec.ActorId();
  const auto &task_id = task_spec.TaskId();
  auto send_pos = task_spec.SequenceNumber();

  // Shouldn't hold a lock while accessing task_manager_.
  // Task is already canceled or finished.
  GetTaskManagerWithoutMu().MarkTaskCanceled(task_id);
  if (!GetTaskManagerWithoutMu().IsTaskPending(task_id)) {
    RAY_LOG(DEBUG).WithField(task_id) << "Task is already finished or canceled";
    return Status::OK();
  }

  auto task_queued = false;
  {
    absl::MutexLock lock(&mu_);

    generators_to_resubmit_.erase(task_id);

    auto queue = client_queues_.find(actor_id);
    RAY_CHECK(queue != client_queues_.end());
    if (queue->second.state == rpc::ActorTableData::DEAD) {
      // No need to decrement cur_pending_calls because it doesn't matter.
      RAY_LOG(DEBUG).WithField(task_id)
          << "Task's actor is already dead. Ignoring the cancel request.";
      return Status::OK();
    }

    task_queued = queue->second.actor_submit_queue->Contains(send_pos);
    if (task_queued) {
      auto dep_resolved =
          queue->second.actor_submit_queue->DependenciesResolved(send_pos);
      if (!dep_resolved) {
        RAY_LOG(DEBUG).WithField(task_id)
            << "Task has been resolving dependencies. Cancel to resolve dependencies";
        resolver_.CancelDependencyResolution(task_id);
      }
      RAY_LOG(DEBUG).WithField(task_id)
          << "Task was queued. Mark a task is canceled from a queue.";
      queue->second.actor_submit_queue->MarkTaskCanceled(send_pos);
    }
  }

  // Fail a request immediately if it is still queued.
  // The task won't be sent to an actor in this case.
  // We cannot hold a lock when calling `FailOrRetryPendingTask`.
  if (task_queued) {
    rpc::RayErrorInfo error_info;
    std::ostringstream stream;
    stream << "The task " << task_id << " is canceled from an actor " << actor_id
           << " before it executes.";
    error_info.set_error_message(stream.str());
    error_info.set_error_type(rpc::ErrorType::TASK_CANCELLED);
    GetTaskManagerWithoutMu().FailOrRetryPendingTask(
        task_id, rpc::ErrorType::TASK_CANCELLED, /*status*/ nullptr, &error_info);
    return Status::OK();
  }

  // At this point, the task is in "sent" state and not finished yet.
  // We cannot guarantee a cancel request is received "after" a task
  // is submitted because gRPC is not ordered. To get around it,
  // we keep retrying cancel RPCs until task is finished or
  // an executor tells us to stop retrying.

  // If there's no client, it means actor is not created yet.
  // Retry in 1 second.
  {
    absl::MutexLock lock(&mu_);
    RAY_LOG(DEBUG).WithField(task_id) << "Task was sent to an actor. Send a cancel RPC.";
    auto queue = client_queues_.find(actor_id);
    RAY_CHECK(queue != client_queues_.end());
    if (!queue->second.rpc_client) {
      RetryCancelTask(task_spec, recursive, 1000);
      return Status::OK();
    }

    const auto &client = queue->second.rpc_client;
    auto request = rpc::CancelTaskRequest();
    request.set_intended_task_id(task_spec.TaskIdBinary());
    request.set_force_kill(force_kill);
    request.set_recursive(recursive);
    request.set_caller_worker_id(task_spec.CallerWorkerIdBinary());
    client->CancelTask(request,
                       [this, task_spec = std::move(task_spec), recursive, task_id](
                           const Status &status, const rpc::CancelTaskReply &reply) {
                         RAY_LOG(DEBUG).WithField(task_spec.TaskId())
                             << "CancelTask RPC response received with status "
                             << status.ToString();

                         // Keep retrying every 2 seconds until a task is officially
                         // finished.
                         if (!GetTaskManagerWithoutMu().GetTaskSpec(task_id)) {
                           // Task is already finished.
                           RAY_LOG(DEBUG).WithField(task_spec.TaskId())
                               << "Task is finished. Stop a cancel request.";
                           return;
                         }

                         if (!reply.attempt_succeeded()) {
                           RetryCancelTask(task_spec, recursive, 2000);
                         }
                       });
  }

  // NOTE: Currently, ray.cancel is asynchronous.
  // If we want to have a better guarantee in the cancelation result
  // we should make it synchronos, but that can regress the performance.
  return Status::OK();
}

bool ActorTaskSubmitter::QueueGeneratorForResubmit(const TaskSpecification &spec) {
  // TODO(dayshah): Needs to integrate with the cancellation logic - what if task was
  // cancelled before this?
  absl::MutexLock lock(&mu_);
  generators_to_resubmit_.insert(spec.TaskId());
  return true;
}

}  // namespace core
}  // namespace ray

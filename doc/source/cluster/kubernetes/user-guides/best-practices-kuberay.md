(best-practices-kuberay)=

# Best practices for dependencies and storage with KubeRay

This document contains recommendations for setting up storage and handling application dependencies for your Ray deployment on Kubernetes.

When you set up Ray on Kubernetes, the KubeRay documentation provides an overview of how to configure the operator to execute and manage the Ray cluster lifecycle.
However, administrators may still have questions with respect to actual day-to-day usage. For example:

* How do I ship or run code on the Ray cluster?
* What type of storage system should I set up for artifacts?
* How do I handle package dependencies for your application?

The answers to these questions will vary between development and production. This table summarizes the recommended setup for both situations:

|   | Interactive Development  | Production  |
|---|---|---|
| Cluster Configuration  | KubeRay YAML  | KubeRay YAML  |
| Code | Run driver or Jupyter notebook on head node | Bake code into Docker image  |
| Artifact Storage | Set up an EFS  | Cloud storage (S3, GS)  |
| Package Dependencies | Install onto NFS <br /> or <br /> Use runtime environments | Bake into docker image  |

Table 1: Table comparing recommended setup for development and production.

## Interactive development

To provide an interactive development environment for data scientists and ML practitioners, we recommend setting up the code, storage, and dependencies in a way that reduces context switches for developers and shortens iteration times.

```{eval-rst}
.. image:: ../images/interactive-dev.png
    :align: center
..
    Find the source document here (https://whimsical.com/clusters-P5Y6R23riCuNb6xwXVXN72)
```

### Storage

Use one of these two standard solutions for artifact and log storage during the development process:

* POSIX-compliant network file storage (like AWS and EFS): This approach is useful when you want to have artifacts or dependencies accessible across different nodes in an interactive fashion. For example, experiment logs of different models trained on different Ray tasks.
* Cloud storage (like AWS S3 or GCP GS): This approach is useful for large artifacts or datasets that you need to access with high throughput.

### Driver script

Run the main (driver) script on the head node of the cluster. Ray Core and library programs often assume that the driver is located on the head node and take advantage of the local storage. For example, Ray Tune will by default generate log files on the head node.

A typical workflow can look like this:

* Start a Jupyter server on the head node
* SSH onto the head node and run the driver script or application there
* Use the Ray Job Submission client to submit code from a local machine onto a cluster

### Dependencies

For local dependencies (for example, if you’re working in a mono-repo), or external dependencies (like a pip package), use one of the following options:

* Put the code and install the packages onto your NFS. The benefit is that you can quickly interact with the rest of the codebase and dependencies without shipping it across a cluster every time.
* Bake remote and local dependencies into a published Docker image for the workers. This is the most common way to deploy applications onto [Kubernetes](https://kube.academy/courses/building-applications-for-kubernetes). 
* Use the `runtime env` with the [Ray Job Submission Client](ray.job_submission.JobSubmissionClient), which can pull down code from S3 or ship code from your local working directory onto the remote cluster.

## Production

Our recommendations regarding production are more aligned with standard Kubernetes best practices. For production, we suggest the following configuration.


```{eval-rst}
.. image:: ../images/production.png
    :align: center
..
    Find the source document here (https://whimsical.com/clusters-P5Y6R23riCuNb6xwXVXN72)
```


### Storage

Reading and writing data and artifacts to cloud storage is the most reliable and observable option for production Ray deployments. 

### Code and Dependencies

Bake your code, remote, and local dependencies into a published Docker image for the workers. This is the most common way to deploy applications onto [Kubernetes](https://kube.academy/courses/building-applications-for-kubernetes).

Using Cloud storage and the `runtime_env` is a less preferred method but still viable. In this case, use the runtime environment option to download zip files containing code and other private modules from cloud storage, in addition to specifying the pip packages needed to run your application.
# (C) Datadog, Inc. 2020-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)

import os.path
import threading
import time

import grpc
import pynvml

from datadog_checks.base import AgentCheck

from .api_pb2 import ListPodResourcesRequest
from .api_pb2_grpc import PodResourcesListerStub

METRIC_PREFIX = "nvml."
SOCKET_PATH = "/var/lib/kubelet/pod-resources/kubelet.sock"
"""Assumed to be a UDS accessible from this running code"""


class NvmlInit(object):
    """Wraps an nvmlInit and an nvmlShutdown inside the same context"""

    def __enter__(self):
        NvmlCheck.N.nvmlInit()

    def __exit__(self, exception_type, exception_value, traceback):
        NvmlCheck.N.nvmlShutdown()


class NvmlCall(object):
    previously_printed_errors = set()
    """Wraps a call and checks for an exception (of any type).

       Why this exists: If a graphics card doesn't support a nvml method, we don't want to spam the logs with just
       that method's errors, but we don't want to never error.  And we don't want to fail all the
       metrics, just the metrics that aren't supported.  This class supports that use case.

       NvmlCall wraps a call and checks for an exception (of any type).  If an exception is raised
       then that error is logged, but only logged once for this type of call
    """

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        pass

    def __exit__(self, exception_type, exception_value, traceback):
        # Do nothing if the exception is not from pynvml or there is no exception
        if traceback is None:
            return False
        if exception_type is not pynvml.NVMLError:
            return False

        # Suppress pynvml exceptions so we can continue
        if self.name in self.previously_printed_errors:
            return True
        self.previously_printed_errors.add(self.name)
        self.log.warning("Unable to execute NVML function: %s: %s", self.name, exception_value)
        return True


class NvmlCheck(AgentCheck):
    __NAMESPACE__ = "nvml"
    N = pynvml
    """The pynvml package, explicitly assigned, used for easy test mocking."""
    known_tags = {}
    """A map of GPU UUIDs to the k8s tags we should assign that GPU."""
    lock = threading.Lock()
    """Lock for the object known_tags."""
    _thread = None
    """Daemon thread updating k8s tag information in the background."""

    def check(self, instance):
        # Start thread once and keep it running in the background
        if self._thread is None:
            self._start_discovery()
        with NvmlInit():
            self.gather(instance)

    def gather(self, instance):
        with NvmlCall("device_count"):
            deviceCount = NvmlCheck.N.nvmlDeviceGetCount()
            self.gauge('device_count', deviceCount)
            for i in range(deviceCount):
                handle = NvmlCheck.N.nvmlDeviceGetHandleByIndex(i)
                uuid = NvmlCheck.N.nvmlDeviceGetUUID(handle)
                # The tags used by https://github.com/NVIDIA/gpu-monitoring-tools/blob/master/exporters/prometheus-dcgm/dcgm-exporter/dcgm-exporter # noqa: E501
                tags = ["gpu:" + str(i)]
                # Appends k8s specific tags
                tags += self.get_tags(uuid)
                self.gather_gpu(handle, tags)

    def gather_gpu(self, handle, tags):
        """Gather metrics for a specific GPU"""
        # Utilization information for a device. Each sample period may be
        # between 1 second and 1/6 second, depending on the product being
        # queried.  Taking names to match
        # https://github.com/NVIDIA/gpu-monitoring-tools/blob/master/exporters/prometheus-dcgm/dcgm-exporter/dcgm-exporter # noqa: E501
        # Documented at https://docs.nvidia.com/deploy/nvml-api/group__nvmlDeviceQueries.html # noqa: E501
        with NvmlCall("util_rate"):
            util = NvmlCheck.N.nvmlDeviceGetUtilizationRates(handle)
            self.gauge('gpu_utilization', util.gpu, tags=tags)
            self.gauge('mem_copy_utilization', util.memory, tags=tags)

        # See https://docs.nvidia.com/deploy/nvml-api/structnvmlMemory__t.html#structnvmlMemory__t
        with NvmlCall("mem_info"):
            mem_info = NvmlCheck.N.nvmlDeviceGetMemoryInfo(handle)
            self.gauge('fb_free', mem_info.free, tags=tags)
            self.gauge('fb_used', mem_info.used, tags=tags)
            self.gauge('fb_total', mem_info.total, tags=tags)

        # See https://docs.nvidia.com/deploy/nvml-api/group__nvmlDeviceQueries.html#group__nvmlDeviceQueries_1g7ef7dff0ff14238d08a19ad7fb23fc87 # noqa: E501
        with NvmlCall("power"):
            power = NvmlCheck.N.nvmlDeviceGetPowerUsage(handle)
            self.gauge('power_usage', power, tags=tags)

        # https://docs.nvidia.com/deploy/nvml-api/group__nvmlDeviceQueries.html#group__nvmlDeviceQueries_1g732ab899b5bd18ac4bfb93c02de4900a
        with NvmlCall("total_energy_consumption"):
            consumption = NvmlCheck.N.nvmlDeviceGetTotalEnergyConsumption(handle)
            self.monotonic_count('total_energy_consumption', consumption, tags=tags)

        # https://docs.nvidia.com/deploy/nvml-api/group__nvmlDeviceQueries.html#group__nvmlDeviceQueries_1ga5c77a2154a20d4e660221d8592d21fb
        with NvmlCall("enc_utilization"):
            encoder_util = NvmlCheck.N.nvmlDeviceGetEncoderUtilization(handle)
            self.gauge('enc_utilization', encoder_util[0], tags=tags)

        # https://docs.nvidia.com/deploy/nvml-api/group__nvmlDeviceQueries.html#group__nvmlDeviceQueries_1g0e3420045bc9d04dc37690f4701ced8a
        with NvmlCall("dec_utilization"):
            dec_util = NvmlCheck.N.nvmlDeviceGetDecoderUtilization(handle)
            self.gauge('dec_utilization', dec_util[0], tags=tags)

        # https://docs.nvidia.com/deploy/nvml-api/group__nvmlDeviceQueries.html#group__nvmlDeviceQueries_1gd86f1c74f81b5ddfaa6cb81b51030c72
        with NvmlCall("pci_through"):
            tx_bytes = NvmlCheck.N.nvmlDeviceGetPcieThroughput(handle, pynvml.NVML_PCIE_UTIL_TX_BYTES)
            rx_bytes = NvmlCheck.N.nvmlDeviceGetPcieThroughput(handle, pynvml.NVML_PCIE_UTIL_RX_BYTES)
            self.monotonic_count('pcie_tx_throughput', tx_bytes, tags=tags)
            self.monotonic_count('pcie_rx_throughput', rx_bytes, tags=tags)

    def _start_discovery(self):
        """Start daemon thread to discover which k8s pod is assigned to a GPU"""
        # type: () -> None
        if not os.path.exists(SOCKET_PATH):
            self.log.info("No kubelet socket at %s.  Not monitoring k8s pod tags", SOCKET_PATH)
            return
        self.log.info("Monitoring kubelet tags at %s", SOCKET_PATH)
        self._thread = threading.Thread(target=self.discover_instances, args=(10,), name=self.name, daemon=True)
        self._thread.daemon = True
        self._thread.start()

    def discover_instances(self, interval):
        try:
            while True:
                self.refresh_tags()
                time.sleep(interval)
        except Exception as ex:
            self.log.error(ex)
        finally:
            self.log.warning("discover_instances finished.  No longer refreshing instance tags")

    def get_tags(self, device_id):
        with self.lock:
            # Note: device ID comes in as bytes, but we get strings from grpc
            return self.known_tags.get(device_id, self.known_tags.get(device_id.decode("utf-8"), []))

    def refresh_tags(self):
        channel = grpc.insecure_channel('unix://' + SOCKET_PATH)
        stub = PodResourcesListerStub(channel)
        response = stub.List(ListPodResourcesRequest())
        new_tags = {}
        for pod_res in response.pod_resources:
            for container in pod_res.containers:
                for device in container.devices:
                    if device.resource_name != "nvidia.com/gpu":
                        continue
                    pod_name = pod_res.name
                    kube_namespace = pod_res.namespace
                    kube_container_name = container.name
                    for device_id in device.device_ids:
                        # These are the tag names that datadog seems to use
                        new_tags[device_id] = [
                            "pod_name:" + pod_name,
                            "kube_namespace:" + kube_namespace,
                            "kube_container_name:" + kube_container_name,
                        ]
        with self.lock:
            self.known_tags = new_tags

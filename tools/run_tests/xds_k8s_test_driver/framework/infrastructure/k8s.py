# Copyright 2020 gRPC authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import datetime
import functools
import json
import logging
import re
import subprocess
import time
from typing import List, Optional, Tuple

from kubernetes import client
from kubernetes import utils
import kubernetes.config
import yaml

from framework.helpers import retryers
import framework.helpers.highlighter

logger = logging.getLogger(__name__)
# Type aliases
_HighlighterYaml = framework.helpers.highlighter.HighlighterYaml
V1Deployment = client.V1Deployment
V1ServiceAccount = client.V1ServiceAccount
V1Pod = client.V1Pod
V1PodList = client.V1PodList
V1Service = client.V1Service
V1Namespace = client.V1Namespace
ApiException = client.ApiException


def simple_resource_get(func):

    def wrap_not_found_return_none(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except client.ApiException as e:
            if e.status == 404:
                # Ignore 404
                return None
            raise

    return wrap_not_found_return_none


def label_dict_to_selector(labels: dict) -> str:
    return ','.join(f'{k}=={v}' for k, v in labels.items())


class KubernetesApiManager:

    def __init__(self, context):
        self.context = context
        self.client = self._cached_api_client_for_context(context)
        self.apps = client.AppsV1Api(self.client)
        self.core = client.CoreV1Api(self.client)

    def close(self):
        self.client.close()

    @classmethod
    @functools.lru_cache(None)
    def _cached_api_client_for_context(cls, context: str) -> client.ApiClient:
        client_instance = kubernetes.config.new_client_from_config(
            context=context)
        logger.info('Using kubernetes context "%s", active host: %s', context,
                    client_instance.configuration.host)
        return client_instance


class PortForwardingError(Exception):
    """Error forwarding port"""


class PortForwarder:
    PORT_FORWARD_LOCAL_ADDRESS: str = '127.0.0.1'

    def __init__(self,
                 context: str,
                 namespace: str,
                 destination: str,
                 remote_port: int,
                 local_port: Optional[int] = None,
                 local_address: Optional[str] = None):
        self.context = context
        self.namespace = namespace
        self.destination = destination
        self.remote_port = remote_port
        self.local_address = local_address or self.PORT_FORWARD_LOCAL_ADDRESS
        self.local_port: Optional[int] = local_port
        self.subprocess: Optional[subprocess.Popen] = None

    def connect(self) -> None:
        if self.local_port:
            port_mapping = f"{self.local_port}:{self.remote_port}"
        else:
            port_mapping = f":{self.remote_port}"
        cmd = [
            "kubectl", "--context", self.context, "--namespace", self.namespace,
            "port-forward", "--address", self.local_address, self.destination,
            port_mapping
        ]
        self.subprocess = subprocess.Popen(cmd,
                                           stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT,
                                           universal_newlines=True)
        # Wait for stdout line indicating successful start.
        if self.local_port:
            local_port_expected = (
                f"Forwarding from {self.local_address}:{self.local_port}"
                f" -> {self.remote_port}")
        else:
            local_port_re = re.compile(
                f"Forwarding from {self.local_address}:([0-9]+) -> {self.remote_port}"
            )
        try:
            while True:
                time.sleep(0.05)
                output = self.subprocess.stdout.readline().strip()
                if not output:
                    return_code = self.subprocess.poll()
                    if return_code is not None:
                        errors = [
                            error
                            for error in self.subprocess.stdout.readlines()
                        ]
                        raise PortForwardingError(
                            'Error forwarding port, kubectl return '
                            f'code {return_code}, output {errors}')
                    # If there is no output, and the subprocess is not exiting,
                    # continue waiting for the log line.
                    continue

                # Validate output log
                if self.local_port:
                    if output != local_port_expected:
                        raise PortForwardingError(
                            f'Error forwarding port, unexpected output {output}'
                        )
                else:
                    groups = local_port_re.search(output)
                    if groups is None:
                        raise PortForwardingError(
                            f'Error forwarding port, unexpected output {output}'
                        )
                    # Update local port to the randomly picked one
                    self.local_port = int(groups[1])

                logger.info(output)
                break
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self.subprocess is not None:
            logger.info('Shutting down port forwarding, pid %s',
                        self.subprocess.pid)
            self.subprocess.kill()
            stdout, _ = self.subprocess.communicate(timeout=5)
            logger.info('Port forwarding stopped')
            logger.debug('Port forwarding remaining stdout: %s', stdout)
            self.subprocess = None


class KubernetesNamespace:  # pylint: disable=too-many-public-methods
    NEG_STATUS_META = 'cloud.google.com/neg-status'
    DELETE_GRACE_PERIOD_SEC: int = 5
    WAIT_SHORT_TIMEOUT_SEC: int = 60
    WAIT_SHORT_SLEEP_SEC: int = 1
    WAIT_MEDIUM_TIMEOUT_SEC: int = 5 * 60
    WAIT_MEDIUM_SLEEP_SEC: int = 10
    WAIT_LONG_TIMEOUT_SEC: int = 10 * 60
    WAIT_LONG_SLEEP_SEC: int = 30

    def __init__(self, api: KubernetesApiManager, name: str):
        self._highlighter = _HighlighterYaml()
        self.name = name
        self.api = api

    def apply_manifest(self, manifest):
        return utils.create_from_dict(self.api.client,
                                      manifest,
                                      namespace=self.name)

    @simple_resource_get
    def get_service(self, name) -> V1Service:
        return self.api.core.read_namespaced_service(name, self.name)

    @simple_resource_get
    def get_service_account(self, name) -> V1Service:
        return self.api.core.read_namespaced_service_account(name, self.name)

    def delete_service(self,
                       name,
                       grace_period_seconds=DELETE_GRACE_PERIOD_SEC):
        self.api.core.delete_namespaced_service(
            name=name,
            namespace=self.name,
            body=client.V1DeleteOptions(
                propagation_policy='Foreground',
                grace_period_seconds=grace_period_seconds))

    def delete_service_account(self,
                               name,
                               grace_period_seconds=DELETE_GRACE_PERIOD_SEC):
        self.api.core.delete_namespaced_service_account(
            name=name,
            namespace=self.name,
            body=client.V1DeleteOptions(
                propagation_policy='Foreground',
                grace_period_seconds=grace_period_seconds))

    @simple_resource_get
    def get(self) -> V1Namespace:
        return self.api.core.read_namespace(self.name)

    def delete(self, grace_period_seconds=DELETE_GRACE_PERIOD_SEC):
        self.api.core.delete_namespace(
            name=self.name,
            body=client.V1DeleteOptions(
                propagation_policy='Foreground',
                grace_period_seconds=grace_period_seconds))

    def wait_for_service_deleted(self,
                                 name: str,
                                 timeout_sec: int = WAIT_SHORT_TIMEOUT_SEC,
                                 wait_sec: int = WAIT_SHORT_SLEEP_SEC) -> None:
        retryer = retryers.constant_retryer(
            wait_fixed=datetime.timedelta(seconds=wait_sec),
            timeout=datetime.timedelta(seconds=timeout_sec),
            check_result=lambda service: service is None)
        retryer(self.get_service, name)

    def wait_for_service_account_deleted(
            self,
            name: str,
            timeout_sec: int = WAIT_SHORT_TIMEOUT_SEC,
            wait_sec: int = WAIT_SHORT_SLEEP_SEC) -> None:
        retryer = retryers.constant_retryer(
            wait_fixed=datetime.timedelta(seconds=wait_sec),
            timeout=datetime.timedelta(seconds=timeout_sec),
            check_result=lambda service_account: service_account is None)
        retryer(self.get_service_account, name)

    def wait_for_namespace_deleted(self,
                                   timeout_sec: int = WAIT_LONG_TIMEOUT_SEC,
                                   wait_sec: int = WAIT_LONG_SLEEP_SEC) -> None:
        retryer = retryers.constant_retryer(
            wait_fixed=datetime.timedelta(seconds=wait_sec),
            timeout=datetime.timedelta(seconds=timeout_sec),
            check_result=lambda namespace: namespace is None)
        retryer(self.get)

    def wait_for_service_neg(self,
                             name: str,
                             timeout_sec: int = WAIT_SHORT_TIMEOUT_SEC,
                             wait_sec: int = WAIT_SHORT_SLEEP_SEC) -> None:
        timeout = datetime.timedelta(seconds=timeout_sec)
        retryer = retryers.constant_retryer(
            wait_fixed=datetime.timedelta(seconds=wait_sec),
            timeout=timeout,
            check_result=self._check_service_neg_annotation)
        try:
            retryer(self.get_service, name)
        except retryers.RetryError as e:
            logger.error(
                'Timeout %s (h:mm:ss) waiting for service %s to report NEG '
                'status. Last service status:\n%s', timeout, name,
                self._pretty_format_status(e.result()))
            raise

    def get_service_neg(self, service_name: str,
                        service_port: int) -> Tuple[str, List[str]]:
        service = self.get_service(service_name)
        neg_info: dict = json.loads(
            service.metadata.annotations[self.NEG_STATUS_META])
        neg_name: str = neg_info['network_endpoint_groups'][str(service_port)]
        neg_zones: List[str] = neg_info['zones']
        return neg_name, neg_zones

    @simple_resource_get
    def get_deployment(self, name) -> V1Deployment:
        return self.api.apps.read_namespaced_deployment(name, self.name)

    def delete_deployment(
            self,
            name: str,
            grace_period_seconds: int = DELETE_GRACE_PERIOD_SEC) -> None:
        self.api.apps.delete_namespaced_deployment(
            name=name,
            namespace=self.name,
            body=client.V1DeleteOptions(
                propagation_policy='Foreground',
                grace_period_seconds=grace_period_seconds))

    def list_deployment_pods(self, deployment: V1Deployment) -> List[V1Pod]:
        # V1LabelSelector.match_expressions not supported at the moment
        return self.list_pods_with_labels(deployment.spec.selector.match_labels)

    def wait_for_deployment_available_replicas(
            self,
            name: str,
            count: int = 1,
            timeout_sec: int = WAIT_MEDIUM_TIMEOUT_SEC,
            wait_sec: int = WAIT_SHORT_SLEEP_SEC) -> None:
        timeout = datetime.timedelta(seconds=timeout_sec)
        retryer = retryers.constant_retryer(
            wait_fixed=datetime.timedelta(seconds=wait_sec),
            timeout=timeout,
            check_result=lambda depl: self._replicas_available(depl, count))
        try:
            retryer(self.get_deployment, name)
        except retryers.RetryError as e:
            logger.error(
                'Timeout %s (h:mm:ss) waiting for deployment %s to report %i '
                'replicas available. Last status:\n%s', timeout, name, count,
                self._pretty_format_status(e.result()))
            raise

    def wait_for_deployment_replica_count(
            self,
            deployment: V1Deployment,
            count: int = 1,
            *,
            timeout_sec: int = WAIT_MEDIUM_TIMEOUT_SEC,
            wait_sec: int = WAIT_SHORT_SLEEP_SEC) -> None:
        timeout = datetime.timedelta(seconds=timeout_sec)
        retryer = retryers.constant_retryer(
            wait_fixed=datetime.timedelta(seconds=wait_sec),
            timeout=timeout,
            check_result=lambda pods: len(pods) == count)
        try:
            retryer(self.list_deployment_pods, deployment)
        except retryers.RetryError as e:
            result = e.result(default=[])
            logger.error(
                'Timeout %s (h:mm:ss) waiting for pod count %i, got: %i. '
                'Pod statuses:\n%s', timeout, count, len(result),
                self._pretty_format_statuses(result))
            raise

    def wait_for_deployment_deleted(
            self,
            deployment_name: str,
            timeout_sec: int = WAIT_MEDIUM_TIMEOUT_SEC,
            wait_sec: int = WAIT_MEDIUM_SLEEP_SEC) -> None:
        retryer = retryers.constant_retryer(
            wait_fixed=datetime.timedelta(seconds=wait_sec),
            timeout=datetime.timedelta(seconds=timeout_sec),
            check_result=lambda deployment: deployment is None)
        retryer(self.get_deployment, deployment_name)

    def list_pods_with_labels(self, labels: dict) -> List[V1Pod]:
        pod_list: V1PodList = self.api.core.list_namespaced_pod(
            self.name, label_selector=label_dict_to_selector(labels))
        return pod_list.items

    def get_pod(self, name: str) -> V1Pod:
        return self.api.core.read_namespaced_pod(name, self.name)

    def wait_for_pod_started(self,
                             pod_name: str,
                             timeout_sec: int = WAIT_SHORT_TIMEOUT_SEC,
                             wait_sec: int = WAIT_SHORT_SLEEP_SEC) -> None:
        timeout = datetime.timedelta(seconds=timeout_sec)
        retryer = retryers.constant_retryer(
            wait_fixed=datetime.timedelta(seconds=wait_sec),
            timeout=timeout,
            check_result=self._pod_started)
        try:
            retryer(self.get_pod, pod_name)
        except retryers.RetryError as e:
            logger.error(
                'Timeout %s (h:mm:ss) waiting for pod %s to start. '
                'Pod status:\n%s', timeout, pod_name,
                self._pretty_format_status(e.result()))
            raise

    def port_forward_pod(
        self,
        pod: V1Pod,
        remote_port: int,
        local_port: Optional[int] = None,
        local_address: Optional[str] = None,
    ) -> PortForwarder:
        pf = PortForwarder(self.api.context, self.name,
                           f"pod/{pod.metadata.name}", remote_port, local_port,
                           local_address)
        pf.connect()
        return pf

    def _pretty_format_statuses(self,
                                k8s_objects: List[Optional[object]]) -> str:
        return '\n'.join(
            self._pretty_format_status(k8s_object)
            for k8s_object in k8s_objects)

    def _pretty_format_status(self, k8s_object: Optional[object]) -> str:
        if k8s_object is None:
            return 'No data'

        # Parse the name if present.
        if hasattr(k8s_object, 'metadata') and hasattr(k8s_object.metadata,
                                                       'name'):
            name = k8s_object.metadata.name
        else:
            name = 'Can\'t parse resource name'

        # Pretty-print the status if present.
        if hasattr(k8s_object, 'status'):
            try:
                status = self._pretty_format(k8s_object.status.to_dict())
            except Exception as e:  # pylint: disable=broad-except
                # Catching all exceptions because not printing the status
                # isn't as important as the system under test.
                status = f'Can\'t parse resource status: {e}'
        else:
            status = 'Can\'t parse resource status'

        # Return the name of k8s object, and its pretty-printed status.
        return f'{name}:\n{status}\n'

    def _pretty_format(self, data: dict) -> str:
        """Return a string with pretty-printed yaml data from a python dict."""
        yaml_out: str = yaml.dump(data, explicit_start=True, explicit_end=True)
        return self._highlighter.highlight(yaml_out)

    @classmethod
    def _check_service_neg_annotation(cls,
                                      service: Optional[V1Service]) -> bool:
        return (isinstance(service, V1Service) and
                cls.NEG_STATUS_META in service.metadata.annotations)

    @classmethod
    def _pod_started(cls, pod: V1Pod) -> bool:
        return (isinstance(pod, V1Pod) and
                pod.status.phase not in ('Pending', 'Unknown'))

    @classmethod
    def _replicas_available(cls, deployment: V1Deployment, count: int) -> bool:
        return (isinstance(deployment, V1Deployment) and
                deployment.status.available_replicas is not None and
                deployment.status.available_replicas >= count)

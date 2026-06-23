# Copyright Amazon.com Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may
# not use this file except in compliance with the License. A copy of the
# License is located at
#
#	 http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed
# on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

import time

import boto3
import pytest

from acktest import k8s
from acktest.k8s import condition
from acktest.k8s import resource as k8s_resource
from acktest.resources import random_suffix_name

from e2e import CRD_GROUP, CRD_VERSION, load_s3tables_resource
from e2e.replacement_values import REPLACEMENT_VALUES

TABLE_BUCKET_PLURAL = "tablebuckets"
NAMESPACE_PLURAL = "namespaces"

CREATE_WAIT_AFTER_SECONDS = 20
DELETE_WAIT_AFTER_SECONDS = 20


def pytest_addoption(parser):
    parser.addoption(
        "--runslow", action="store_true", default=False, help="run slow tests"
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "service(arg): mark test associated with a given service"
    )
    config.addinivalue_line("markers", "slow: mark test as slow to run")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--runslow"):
        return
    skip_slow = pytest.mark.skip(reason="need --runslow option to run")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


# Provide a k8s client to interact with the integration test cluster
@pytest.fixture(scope="class")
def k8s_client():
    return k8s._get_k8s_api_client()


@pytest.fixture(scope="module")
def s3tables_client():
    return boto3.client("s3tables")


@pytest.fixture
def table_bucket():
    """Provisions a TableBucket and yields its details for resources that
    reference it. Tears the bucket down after the test.

    Yields a dict with:
      - cr_name: the CR (and bucket) name, usable as TABLE_BUCKET_CR_NAME
      - ref: the CustomResourceReference
      - arn: the AWS table bucket ARN
    """
    table_bucket_name = random_suffix_name("ack-test-bucket", 32)
    replacements = REPLACEMENT_VALUES.copy()
    replacements["TABLE_BUCKET_NAME"] = table_bucket_name
    resource_data = load_s3tables_resource(
        "table_bucket", additional_replacements=replacements
    )
    ref = k8s_resource.CustomResourceReference(
        CRD_GROUP, CRD_VERSION, TABLE_BUCKET_PLURAL,
        table_bucket_name, namespace="default",
    )
    k8s_resource.create_custom_resource(ref, resource_data)
    k8s_resource.wait_resource_consumed_by_controller(ref)
    time.sleep(CREATE_WAIT_AFTER_SECONDS)
    assert k8s_resource.wait_on_condition(
        ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=20,
    )
    cr = k8s_resource.get_resource(ref)
    arn = cr["status"]["ackResourceMetadata"]["arn"]

    yield {"cr_name": table_bucket_name, "ref": ref, "arn": arn}

    k8s_resource.delete_custom_resource(ref)
    time.sleep(DELETE_WAIT_AFTER_SECONDS)


@pytest.fixture
def namespace(table_bucket):
    """Provisions a Namespace inside the `table_bucket` fixture and yields its
    details for resources that reference it. Tears the namespace down after the
    test (before the parent bucket).

    Yields a dict with:
      - name: the S3 Tables namespace name (matches ^[0-9a-z_]*$)
      - cr_name: the CR name, usable as NAMESPACE_CR_NAME
      - ref: the CustomResourceReference
      - table_bucket: the parent `table_bucket` fixture value
    """
    namespace_name = random_suffix_name("ack_test_ns", 24).replace("-", "_")
    cr_name = namespace_name.replace("_", "-")
    replacements = REPLACEMENT_VALUES.copy()
    replacements["NAMESPACE_NAME"] = namespace_name
    replacements["NAMESPACE_CR_NAME"] = cr_name
    replacements["TABLE_BUCKET_CR_NAME"] = table_bucket["cr_name"]
    resource_data = load_s3tables_resource(
        "namespace", additional_replacements=replacements
    )
    ref = k8s_resource.CustomResourceReference(
        CRD_GROUP, CRD_VERSION, NAMESPACE_PLURAL,
        cr_name, namespace="default",
    )
    k8s_resource.create_custom_resource(ref, resource_data)
    k8s_resource.wait_resource_consumed_by_controller(ref)
    time.sleep(CREATE_WAIT_AFTER_SECONDS)
    assert k8s_resource.wait_on_condition(
        ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=20,
    )

    yield {
        "name": namespace_name,
        "cr_name": cr_name,
        "ref": ref,
        "table_bucket": table_bucket,
    }

    k8s_resource.delete_custom_resource(ref)
    time.sleep(DELETE_WAIT_AFTER_SECONDS)

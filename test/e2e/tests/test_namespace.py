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
"""Integration tests for the S3 Tables Namespace resource."""

import time

import pytest

from acktest.k8s import condition
from acktest.k8s import resource as k8s
from acktest.resources import random_suffix_name

from e2e import (
    CRD_GROUP,
    CRD_VERSION,
    load_s3tables_resource,
    service_marker,
)
from e2e.replacement_values import REPLACEMENT_VALUES

NAMESPACE_PLURAL = "namespaces"

CREATE_WAIT_AFTER_SECONDS = 20
DELETE_WAIT_AFTER_SECONDS = 20


def get_namespace(s3tables_client, table_bucket_arn: str, name: str):
    """Returns the namespace from AWS, or None if it does not exist."""
    try:
        return s3tables_client.get_namespace(
            tableBucketARN=table_bucket_arn, namespace=name
        )
    except s3tables_client.exceptions.NotFoundException:
        return None


@service_marker
@pytest.mark.canary
class TestNamespace:
    def test_create_delete(self, s3tables_client, table_bucket):
        # The parent TableBucket is provisioned (and torn down) by the
        # `table_bucket` fixture; the Namespace references it.
        table_bucket_arn = table_bucket["arn"]

        # Namespace names must match ^[0-9a-z_]*$ (no hyphens).
        namespace_name = random_suffix_name("ack_test_ns", 24).replace("-", "_")
        cr_name = namespace_name.replace("_", "-")

        replacements = REPLACEMENT_VALUES.copy()
        replacements["NAMESPACE_NAME"] = namespace_name
        replacements["NAMESPACE_CR_NAME"] = cr_name
        replacements["TABLE_BUCKET_CR_NAME"] = table_bucket["cr_name"]

        ns_data = load_s3tables_resource(
            "namespace", additional_replacements=replacements
        )
        ns_ref = k8s.CustomResourceReference(
            CRD_GROUP, CRD_VERSION, NAMESPACE_PLURAL,
            cr_name, namespace="default",
        )
        k8s.create_custom_resource(ns_ref, ns_data)
        cr = k8s.wait_resource_consumed_by_controller(ns_ref)

        assert cr is not None
        assert k8s.get_resource_exists(ns_ref)

        time.sleep(CREATE_WAIT_AFTER_SECONDS)

        assert k8s.wait_on_condition(
            ns_ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=10,
        )

        cr = k8s.get_resource(ns_ref)
        assert "status" in cr
        assert cr["status"]["ackResourceMetadata"]["ownerAccountID"] is not None
        # AWS-assigned status fields populated from GetNamespace.
        assert cr["status"].get("createdAt") is not None
        assert cr["status"].get("namespaceID") is not None
        assert cr["status"].get("tableBucketID") is not None

        # Verify in AWS. GetNamespace returns the namespace as a list.
        aws_ns = get_namespace(s3tables_client, table_bucket_arn, namespace_name)
        assert aws_ns is not None
        assert aws_ns["namespace"] == [namespace_name]

        # Delete the namespace and confirm it is gone from AWS. (Namespace
        # has no mutable fields, so there is no update phase.)
        _, deleted = k8s.delete_custom_resource(ns_ref)
        assert deleted
        time.sleep(DELETE_WAIT_AFTER_SECONDS)
        assert get_namespace(s3tables_client, table_bucket_arn, namespace_name) is None

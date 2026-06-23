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
"""Integration tests for the S3 Tables Table resource."""

import json
import time

import pytest

from acktest import tags
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

TABLE_PLURAL = "tables"

CREATE_WAIT_AFTER_SECONDS = 20
MODIFY_WAIT_AFTER_SECONDS = 20
DELETE_WAIT_AFTER_SECONDS = 20


def get_table(s3tables_client, table_bucket_arn: str, namespace: str, name: str):
    """Returns the table from AWS, or None if it does not exist."""
    try:
        return s3tables_client.get_table(
            tableBucketARN=table_bucket_arn, namespace=namespace, name=name
        )
    except s3tables_client.exceptions.NotFoundException:
        return None


@service_marker
@pytest.mark.canary
class TestTable:
    def test_create_update_delete(self, s3tables_client, namespace):
        # The parent TableBucket and Namespace are provisioned (and torn down)
        # by the `namespace` fixture; the Table references both.
        table_bucket = namespace["table_bucket"]
        table_bucket_arn = table_bucket["arn"]
        namespace_name = namespace["name"]

        table_ref = None
        try:
            # --- Create the Table ---
            table_name = random_suffix_name("ack_test_tbl", 24).replace("-", "_")
            table_cr_name = table_name.replace("_", "-")
            replacements = REPLACEMENT_VALUES.copy()
            replacements["TABLE_NAME"] = table_name
            replacements["TABLE_CR_NAME"] = table_cr_name
            replacements["TABLE_BUCKET_CR_NAME"] = table_bucket["cr_name"]
            replacements["NAMESPACE_CR_NAME"] = namespace["cr_name"]

            table_data = load_s3tables_resource(
                "table", additional_replacements=replacements
            )
            table_ref = k8s.CustomResourceReference(
                CRD_GROUP, CRD_VERSION, TABLE_PLURAL,
                table_cr_name, namespace="default",
            )
            k8s.create_custom_resource(table_ref, table_data)
            cr = k8s.wait_resource_consumed_by_controller(table_ref)

            assert cr is not None
            assert k8s.get_resource_exists(table_ref)

            time.sleep(CREATE_WAIT_AFTER_SECONDS)

            assert k8s.wait_on_condition(
                table_ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=20,
            )

            cr = k8s.get_resource(table_ref)
            assert "status" in cr
            assert cr["status"]["ackResourceMetadata"]["ownerAccountID"] is not None
            assert cr["status"].get("createdAt") is not None
            assert cr["status"].get("versionToken") is not None
            assert cr["status"].get("namespaceID") is not None
            assert cr["status"].get("tableBucketID") is not None

            # Verify in AWS.
            aws_table = get_table(
                s3tables_client, table_bucket_arn, namespace_name, table_name
            )
            assert aws_table is not None
            assert aws_table["name"] == table_name
            assert aws_table["format"] == "ICEBERG"

            table_arn = cr["status"]["ackResourceMetadata"]["arn"]

            # Tags applied at creation.
            resource_tags = s3tables_client.list_tags_for_resource(resourceArn=table_arn)["tags"]
            tags.assert_present({"environment": "test", "team": "ack"}, resource_tags)

            # --- Update: tags ---
            tag_updates = {
                "spec": {
                    "tags": {
                        "environment": "prod",
                        "owner": "platform",
                        "team": None,
                    },
                },
            }
            k8s.patch_custom_resource(table_ref, tag_updates)
            time.sleep(MODIFY_WAIT_AFTER_SECONDS)

            assert k8s.wait_on_condition(
                table_ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=20,
            )

            resource_tags = s3tables_client.list_tags_for_resource(resourceArn=table_arn)["tags"]
            tags.assert_equal_without_ack_tags(
                {"environment": "prod", "owner": "platform"}, resource_tags
            )

            # --- Update: rename (exercises RenameTable + versionToken guard) ---
            new_table_name = random_suffix_name("ack_test_tbl", 24).replace("-", "_")
            rename_updates = {"spec": {"name": new_table_name}}
            k8s.patch_custom_resource(table_ref, rename_updates)
            time.sleep(MODIFY_WAIT_AFTER_SECONDS)

            assert k8s.wait_on_condition(
                table_ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=20,
            )

            # The new name resolves in AWS; the old name no longer exists.
            assert get_table(
                s3tables_client, table_bucket_arn, namespace_name, new_table_name
            ) is not None
            assert get_table(
                s3tables_client, table_bucket_arn, namespace_name, table_name
            ) is None
            table_name = new_table_name

            # --- Delete ---
            _, deleted = k8s.delete_custom_resource(table_ref)
            assert deleted
            time.sleep(DELETE_WAIT_AFTER_SECONDS)
            assert get_table(
                s3tables_client, table_bucket_arn, namespace_name, table_name
            ) is None
        finally:
            # The parent Namespace and TableBucket are torn down by the
            # `namespace` fixture; ensure the Table is removed even on failure.
            if table_ref is not None and k8s.get_resource_exists(table_ref):
                k8s.delete_custom_resource(table_ref)
                time.sleep(DELETE_WAIT_AFTER_SECONDS)

    def test_adopt(self, s3tables_client, namespace):
        # Adoption exercises the read-by-name-triplet path in sdkFind: on an
        # adopted resource the ARN is not yet in status, so the read hook falls
        # back to tableBucketARN + namespace + name (rather than tableARN).
        table_bucket = namespace["table_bucket"]
        table_bucket_arn = table_bucket["arn"]
        namespace_name = namespace["name"]

        # Create a table out-of-band (directly in AWS) for the controller to
        # adopt. ICEBERG requires a schema with at least one field.
        table_name = random_suffix_name("ack_test_adopt", 24).replace("-", "_")
        s3tables_client.create_table(
            tableBucketARN=table_bucket_arn,
            namespace=namespace_name,
            name=table_name,
            format="ICEBERG",
            metadata={
                "iceberg": {
                    "schema": {
                        "fields": [
                            {"name": "id", "type": "int"},
                            {"name": "data", "type": "string"},
                        ]
                    }
                }
            },
        )

        adopt_ref = None
        try:
            adopt_cr_name = table_name.replace("_", "-")
            # The yaml template wraps this in double quotes, so escape the JSON
            # quotes to keep the manifest valid YAML after substitution.
            adoption_fields = json.dumps({
                "name": table_name,
                "namespace": namespace_name,
                "tableBucketARN": table_bucket_arn,
            }).replace('"', '\\"')
            replacements = REPLACEMENT_VALUES.copy()
            replacements["TABLE_ADOPTION_CR_NAME"] = adopt_cr_name
            replacements["ADOPTION_FIELDS"] = adoption_fields

            adopt_data = load_s3tables_resource(
                "table_adoption", additional_replacements=replacements
            )
            adopt_ref = k8s.CustomResourceReference(
                CRD_GROUP, CRD_VERSION, TABLE_PLURAL,
                adopt_cr_name, namespace="default",
            )
            k8s.create_custom_resource(adopt_ref, adopt_data)
            k8s.wait_resource_consumed_by_controller(adopt_ref)
            time.sleep(CREATE_WAIT_AFTER_SECONDS)

            assert k8s.wait_on_condition(
                adopt_ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=20,
            )

            # The adopted resource should have its spec/status populated from
            # the existing AWS table (read via the name triplet).
            cr = k8s.get_resource(adopt_ref)
            assert cr["spec"]["name"] == table_name
            assert cr["status"]["ackResourceMetadata"]["arn"] is not None
            assert cr["status"].get("versionToken") is not None

            # Deleting the CR removes the K8s object. The adoption manifest sets
            # deletion-policy: retain, so the underlying AWS table is preserved
            # (it's cleaned up out-of-band in the finally block).
            _, deleted = k8s.delete_custom_resource(adopt_ref)
            assert deleted
            time.sleep(DELETE_WAIT_AFTER_SECONDS)
            assert get_table(
                s3tables_client, table_bucket_arn, namespace_name, table_name
            ) is not None
            adopt_ref = None
        finally:
            if adopt_ref is not None and k8s.get_resource_exists(adopt_ref):
                k8s.delete_custom_resource(adopt_ref)
                time.sleep(DELETE_WAIT_AFTER_SECONDS)
            # Best-effort cleanup if the table still exists in AWS.
            if get_table(s3tables_client, table_bucket_arn, namespace_name, table_name):
                try:
                    s3tables_client.delete_table(
                        tableBucketARN=table_bucket_arn,
                        namespace=namespace_name,
                        name=table_name,
                    )
                except Exception:
                    pass

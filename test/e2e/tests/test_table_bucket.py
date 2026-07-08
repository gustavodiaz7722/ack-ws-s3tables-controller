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
"""Integration tests for the S3 Tables TableBucket resource."""

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

RESOURCE_PLURAL = "tablebuckets"

CREATE_WAIT_AFTER_SECONDS = 20
MODIFY_WAIT_AFTER_SECONDS = 20
DELETE_WAIT_AFTER_SECONDS = 20


def get_table_bucket(s3tables_client, arn: str):
    """Returns the table bucket from AWS, or None if it does not exist."""
    try:
        return s3tables_client.get_table_bucket(tableBucketARN=arn)
    except s3tables_client.exceptions.NotFoundException:
        return None


@pytest.fixture
def adopted_bucket_arns(s3tables_client):
    """Tracks table buckets created out-of-band by adoption tests and reaps any
    survivors at teardown.

    Adoption tests create a bucket directly in AWS (with deletion-policy:
    retain, so the CR teardown leaves the bucket in place) and rely on an
    in-test cleanup. Table buckets are capped by a low per-account quota, so a
    test that errors or whose worker is interrupted before its own cleanup runs
    would strand a bucket and, over many runs, exhaust the quota and break
    unrelated create tests. This teardown deletes only the exact ARNs the test
    registered here, so it never touches buckets owned by other concurrent CI
    jobs sharing the account.
    """
    arns = []
    yield arns
    for arn in arns:
        if get_table_bucket(s3tables_client, arn) is not None:
            try:
                s3tables_client.delete_table_bucket(tableBucketARN=arn)
            except Exception:
                pass


@service_marker
@pytest.mark.canary
class TestTableBucket:
    def test_create_update_delete(self, s3tables_client):
        table_bucket_name = random_suffix_name("ack-test-bucket", 32)

        replacements = REPLACEMENT_VALUES.copy()
        replacements["TABLE_BUCKET_NAME"] = table_bucket_name

        resource_data = load_s3tables_resource(
            "table_bucket",
            additional_replacements=replacements,
        )

        ref = k8s.CustomResourceReference(
            CRD_GROUP,
            CRD_VERSION,
            RESOURCE_PLURAL,
            table_bucket_name,
            namespace="default",
        )
        k8s.create_custom_resource(ref, resource_data)
        cr = k8s.wait_resource_consumed_by_controller(ref)

        assert cr is not None
        assert k8s.get_resource_exists(ref)

        time.sleep(CREATE_WAIT_AFTER_SECONDS)

        # The resource should reach a Synced=True condition.
        assert k8s.wait_on_condition(
            ref,
            condition.CONDITION_TYPE_RESOURCE_SYNCED,
            "True",
            wait_periods=10,
        )

        cr = k8s.get_resource(ref)
        assert "status" in cr
        assert "ackResourceMetadata" in cr["status"]
        arn = cr["status"]["ackResourceMetadata"]["arn"]
        assert arn is not None

        # AWS-assigned status fields populated from GetTableBucket and the
        # standard ACKResourceMetadata. The ownerAccountID of an ACK-managed
        # resource lives under the common ackResourceMetadata block.
        assert cr["status"]["ackResourceMetadata"]["ownerAccountID"] is not None
        assert cr["status"].get("createdAt") is not None
        assert cr["status"].get("tableBucketID") is not None

        # Verify the table bucket exists in AWS.
        aws_bucket = get_table_bucket(s3tables_client, arn)
        assert aws_bucket is not None
        assert aws_bucket["name"] == table_bucket_name

        # Tags supplied at creation should be applied in AWS.
        resource_tags = s3tables_client.list_tags_for_resource(resourceArn=arn)["tags"]
        tags.assert_present({"environment": "test", "team": "ack"}, resource_tags)

        # The bucket should start with the default STANDARD storage class.
        sc = s3tables_client.get_table_bucket_storage_class(tableBucketARN=arn)
        assert sc["storageClassConfiguration"]["storageClass"] == "STANDARD"

        # Update 1: set a non-default storage class via the dedicated Put API
        # (exercises customUpdateTableBucket / syncStorageClass).
        updates = {
            "spec": {
                "storageClassConfiguration": {
                    "storageClass": "INTELLIGENT_TIERING",
                },
            },
        }
        k8s.patch_custom_resource(ref, updates)
        time.sleep(MODIFY_WAIT_AFTER_SECONDS)

        assert k8s.wait_on_condition(
            ref,
            condition.CONDITION_TYPE_RESOURCE_SYNCED,
            "True",
            wait_periods=10,
        )

        # Verify the storage class change is reflected in AWS.
        sc = s3tables_client.get_table_bucket_storage_class(tableBucketARN=arn)
        assert sc["storageClassConfiguration"]["storageClass"] == "INTELLIGENT_TIERING"

        # Update 2: change the encryption configuration (exercises
        # customUpdateTableBucket / syncEncryption -> PutTableBucketEncryption).
        # Set SSE-S3 (AES256) explicitly. We do not test SSE-KMS here because
        # S3 Tables requires a valid customer-managed KMS key ARN for aws:kms,
        # which the test bootstrap does not provision.
        enc_updates = {
            "spec": {
                "encryptionConfiguration": {
                    "sseAlgorithm": "AES256",
                },
            },
        }
        k8s.patch_custom_resource(ref, enc_updates)
        time.sleep(MODIFY_WAIT_AFTER_SECONDS)

        assert k8s.wait_on_condition(
            ref,
            condition.CONDITION_TYPE_RESOURCE_SYNCED,
            "True",
            wait_periods=10,
        )

        enc = s3tables_client.get_table_bucket_encryption(tableBucketARN=arn)
        assert enc["encryptionConfiguration"]["sseAlgorithm"] == "AES256"

        # Update 3: change tags. The CR is patched with a JSON merge patch, so a
        # tag is only removed when its key is explicitly set to null. We change
        # an existing tag's value (environment), add a new tag (owner), and
        # remove a tag (team -> null). This exercises both TagResource and
        # UntagResource without looping.
        tag_updates = {
            "spec": {
                "tags": {
                    "environment": "prod",
                    "owner": "platform",
                    "team": None,
                },
            },
        }
        k8s.patch_custom_resource(ref, tag_updates)
        time.sleep(MODIFY_WAIT_AFTER_SECONDS)

        assert k8s.wait_on_condition(
            ref,
            condition.CONDITION_TYPE_RESOURCE_SYNCED,
            "True",
            wait_periods=10,
        )

        # After the update only the user-defined tags below should remain
        # (ignoring the controller-managed `services.k8s.aws/*` tags).
        resource_tags = s3tables_client.list_tags_for_resource(resourceArn=arn)["tags"]
        tags.assert_equal_without_ack_tags(
            {"environment": "prod", "owner": "platform"}, resource_tags
        )

        # Delete the resource and confirm it is removed from AWS.
        _, deleted = k8s.delete_custom_resource(ref)
        assert deleted
        time.sleep(DELETE_WAIT_AFTER_SECONDS)

        assert get_table_bucket(s3tables_client, arn) is None

    def test_adopt(self, s3tables_client, adopted_bucket_arns):
        # GetTableBucket is keyed by the bucket ARN, so the bucket is adopted by
        # ARN: the adoption-fields annotation carries the ARN, which ACK places
        # into Status.ackResourceMetadata so ReadOne can find the existing bucket.
        table_bucket_name = random_suffix_name("ack-test-adopt", 32)

        # Create a bucket out-of-band (directly in AWS) for the controller to
        # adopt. Register it for teardown immediately so the bucket is reaped
        # even if this test errors before its own cleanup runs (the adoption
        # manifest uses deletion-policy: retain, so nothing else removes it).
        create_resp = s3tables_client.create_table_bucket(name=table_bucket_name)
        arn = create_resp["arn"]
        adopted_bucket_arns.append(arn)

        adopt_ref = None
        try:
            adopt_cr_name = table_bucket_name
            # The yaml template wraps this in double quotes, so escape the JSON
            # quotes to keep the manifest valid YAML after substitution.
            adoption_fields = json.dumps({
                "arn": arn,
            }).replace('"', '\\"')
            replacements = REPLACEMENT_VALUES.copy()
            replacements["TABLE_BUCKET_ADOPTION_CR_NAME"] = adopt_cr_name
            replacements["ADOPTION_FIELDS"] = adoption_fields

            adopt_data = load_s3tables_resource(
                "table_bucket_adoption", additional_replacements=replacements
            )
            adopt_ref = k8s.CustomResourceReference(
                CRD_GROUP, CRD_VERSION, RESOURCE_PLURAL,
                adopt_cr_name, namespace="default",
            )
            k8s.create_custom_resource(adopt_ref, adopt_data)
            k8s.wait_resource_consumed_by_controller(adopt_ref)
            time.sleep(CREATE_WAIT_AFTER_SECONDS)

            assert k8s.wait_on_condition(
                adopt_ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True",
                wait_periods=20,
            )

            # The adopted resource should have its spec/status populated from
            # the existing AWS bucket: ARN in status, name read back into spec.
            cr = k8s.get_resource(adopt_ref)
            assert cr["spec"]["name"] == table_bucket_name
            assert cr["status"]["ackResourceMetadata"]["arn"] == arn

            # Deleting the CR removes the K8s object. The adoption manifest sets
            # deletion-policy: retain, so the underlying AWS bucket is preserved.
            _, deleted = k8s.delete_custom_resource(adopt_ref)
            assert deleted
            time.sleep(DELETE_WAIT_AFTER_SECONDS)

            assert get_table_bucket(s3tables_client, arn) is not None
            adopt_ref = None
        finally:
            # Remove the K8s object if the test left it behind. The underlying
            # AWS bucket is reaped by the adopted_bucket_arns fixture.
            if adopt_ref is not None and k8s.get_resource_exists(adopt_ref):
                k8s.delete_custom_resource(adopt_ref)
                time.sleep(DELETE_WAIT_AFTER_SECONDS)

    def test_policy(self, s3tables_client):
        # Bucket resource policy is a separate API
        # (Put/Get/DeleteTableBucketPolicy), wired via the read hook (late-init)
        # and customUpdateTableBucket.
        table_bucket_name = random_suffix_name("ack-test-policy", 32)

        replacements = REPLACEMENT_VALUES.copy()
        replacements["TABLE_BUCKET_NAME"] = table_bucket_name
        resource_data = load_s3tables_resource(
            "table_bucket", additional_replacements=replacements
        )

        ref = k8s.CustomResourceReference(
            CRD_GROUP, CRD_VERSION, RESOURCE_PLURAL, table_bucket_name,
            namespace="default",
        )
        k8s.create_custom_resource(ref, resource_data)
        k8s.wait_resource_consumed_by_controller(ref)
        time.sleep(CREATE_WAIT_AFTER_SECONDS)
        assert k8s.wait_on_condition(
            ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=10,
        )
        arn = k8s.get_resource(ref)["status"]["ackResourceMetadata"]["arn"]
        account_id = arn.split(":")[4]

        # Set a resource policy via PutTableBucketPolicy.
        policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
                "Action": "s3tables:GetTableBucket",
                "Resource": arn,
            }],
        }
        k8s.patch_custom_resource(ref, {"spec": {"policy": json.dumps(policy)}})
        time.sleep(MODIFY_WAIT_AFTER_SECONDS)
        assert k8s.wait_on_condition(
            ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=10,
        )

        live = json.loads(
            s3tables_client.get_table_bucket_policy(tableBucketARN=arn)["resourcePolicy"]
        )
        assert live["Statement"][0]["Action"] == "s3tables:GetTableBucket"

        # Clear the policy; it should be removed via DeleteTableBucketPolicy.
        k8s.patch_custom_resource(ref, {"spec": {"policy": None}})
        time.sleep(MODIFY_WAIT_AFTER_SECONDS)
        assert k8s.wait_on_condition(
            ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=10,
        )
        try:
            s3tables_client.get_table_bucket_policy(tableBucketARN=arn)
            assert False, "expected NotFoundException after policy removal"
        except s3tables_client.exceptions.NotFoundException:
            pass

        _, deleted = k8s.delete_custom_resource(ref)
        assert deleted
        time.sleep(DELETE_WAIT_AFTER_SECONDS)
        assert get_table_bucket(s3tables_client, arn) is None

    def test_maintenance_configuration(self, s3tables_client):
        # Bucket-level maintenance config is a separate API
        # (Put/GetTableBucketMaintenanceConfiguration), wired via the read hook
        # and customUpdateTableBucket.
        table_bucket_name = random_suffix_name("ack-test-maint", 32)

        replacements = REPLACEMENT_VALUES.copy()
        replacements["TABLE_BUCKET_NAME"] = table_bucket_name
        resource_data = load_s3tables_resource(
            "table_bucket", additional_replacements=replacements
        )

        ref = k8s.CustomResourceReference(
            CRD_GROUP, CRD_VERSION, RESOURCE_PLURAL, table_bucket_name,
            namespace="default",
        )
        k8s.create_custom_resource(ref, resource_data)
        k8s.wait_resource_consumed_by_controller(ref)
        time.sleep(CREATE_WAIT_AFTER_SECONDS)
        assert k8s.wait_on_condition(
            ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=10,
        )
        arn = k8s.get_resource(ref)["status"]["ackResourceMetadata"]["arn"]

        # Change the unreferenced-file-removal settings via the dedicated API.
        updates = {
            "spec": {
                "maintenanceConfiguration": {
                    "icebergUnreferencedFileRemoval": {
                        "status": "enabled",
                        "settings": {
                            "icebergUnreferencedFileRemoval": {
                                "unreferencedDays": 7,
                                "nonCurrentDays": 5,
                            },
                        },
                    },
                },
            },
        }
        k8s.patch_custom_resource(ref, updates)
        time.sleep(MODIFY_WAIT_AFTER_SECONDS)
        assert k8s.wait_on_condition(
            ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=10,
        )

        cfg = s3tables_client.get_table_bucket_maintenance_configuration(
            tableBucketARN=arn
        )["configuration"]
        settings = cfg["icebergUnreferencedFileRemoval"]["settings"][
            "icebergUnreferencedFileRemoval"
        ]
        assert settings["unreferencedDays"] == 7
        assert settings["nonCurrentDays"] == 5

        # Clearing the field leaves the config alone (the field is
        # late-initialized): the service default is preserved rather than
        # disabled, so maintenance stays enabled. To turn it off, status must be
        # set to disabled explicitly.
        k8s.patch_custom_resource(ref, {"spec": {"maintenanceConfiguration": None}})
        time.sleep(MODIFY_WAIT_AFTER_SECONDS)
        assert k8s.wait_on_condition(
            ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=10,
        )
        cfg = s3tables_client.get_table_bucket_maintenance_configuration(
            tableBucketARN=arn
        )["configuration"]
        assert cfg["icebergUnreferencedFileRemoval"]["status"] == "enabled"

        # Disable maintenance explicitly via status=disabled (there is no delete
        # API; the config always exists).
        disable = {
            "spec": {
                "maintenanceConfiguration": {
                    "icebergUnreferencedFileRemoval": {"status": "disabled"},
                },
            },
        }
        k8s.patch_custom_resource(ref, disable)
        time.sleep(MODIFY_WAIT_AFTER_SECONDS)
        assert k8s.wait_on_condition(
            ref, condition.CONDITION_TYPE_RESOURCE_SYNCED, "True", wait_periods=10,
        )
        cfg = s3tables_client.get_table_bucket_maintenance_configuration(
            tableBucketARN=arn
        )["configuration"]
        assert cfg["icebergUnreferencedFileRemoval"]["status"] == "disabled"

        _, deleted = k8s.delete_custom_resource(ref)
        assert deleted
        time.sleep(DELETE_WAIT_AFTER_SECONDS)
        assert get_table_bucket(s3tables_client, arn) is None

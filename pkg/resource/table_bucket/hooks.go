// Copyright Amazon.com Inc. or its affiliates. All Rights Reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License"). You may
// not use this file except in compliance with the License. A copy of the
// License is located at
//
//     http://aws.amazon.com/apache2.0/
//
// or in the "license" file accompanying this file. This file is distributed
// on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
// express or implied. See the License for the specific language governing
// permissions and limitations under the License.

package table_bucket

import (
	"context"
	"errors"

	ackcompare "github.com/aws-controllers-k8s/runtime/pkg/compare"
	ackrtlog "github.com/aws-controllers-k8s/runtime/pkg/runtime/log"
	svcapitypes "github.com/aws-controllers-k8s/s3tables-controller/apis/v1alpha1"
	"github.com/aws/aws-sdk-go-v2/aws"
	svcsdk "github.com/aws/aws-sdk-go-v2/service/s3tables"
	svcsdktypes "github.com/aws/aws-sdk-go-v2/service/s3tables/types"
)

// arnFromKO returns the resource ARN string pointer from the resource's status
// metadata, or nil if it has not yet been populated.
func arnFromKO(ko *svcapitypes.TableBucket) *string {
	if ko.Status.ACKResourceMetadata == nil || ko.Status.ACKResourceMetadata.ARN == nil {
		return nil
	}
	return (*string)(ko.Status.ACKResourceMetadata.ARN)
}

// setBucketConfigurations augments the TableBucket spec with the bucket-level
// encryption and storage class configuration, plus the resource tags. None of
// these are returned by GetTableBucket:
//   - encryption and storage class have dedicated GetTableBucket* APIs
//   - tags have a dedicated ListTagsForResource API
func (rm *resourceManager) setBucketConfigurations(
	ctx context.Context,
	ko *svcapitypes.TableBucket,
) (err error) {
	rlog := ackrtlog.FromContext(ctx)
	exit := rlog.Trace("rm.setBucketConfigurations")
	defer func() { exit(err) }()

	arn := arnFromKO(ko)
	if arn == nil {
		return nil
	}

	encResp, err := rm.sdkapi.GetTableBucketEncryption(
		ctx,
		&svcsdk.GetTableBucketEncryptionInput{TableBucketARN: arn},
	)
	rm.metrics.RecordAPICall("READ_ONE", "GetTableBucketEncryption", err)
	if err != nil {
		return err
	}
	if encResp.EncryptionConfiguration != nil {
		ko.Spec.EncryptionConfiguration = &svcapitypes.EncryptionConfiguration{
			KMSKeyARN: encResp.EncryptionConfiguration.KmsKeyArn,
		}
		if encResp.EncryptionConfiguration.SseAlgorithm != "" {
			ko.Spec.EncryptionConfiguration.SSEAlgorithm = aws.String(
				string(encResp.EncryptionConfiguration.SseAlgorithm),
			)
		}
	} else {
		ko.Spec.EncryptionConfiguration = nil
	}

	scResp, err := rm.sdkapi.GetTableBucketStorageClass(
		ctx,
		&svcsdk.GetTableBucketStorageClassInput{TableBucketARN: arn},
	)
	rm.metrics.RecordAPICall("READ_ONE", "GetTableBucketStorageClass", err)
	if err != nil {
		return err
	}
	if scResp.StorageClassConfiguration != nil &&
		scResp.StorageClassConfiguration.StorageClass != "" {
		ko.Spec.StorageClassConfiguration = &svcapitypes.StorageClassConfiguration{
			StorageClass: aws.String(string(scResp.StorageClassConfiguration.StorageClass)),
		}
	} else {
		ko.Spec.StorageClassConfiguration = nil
	}

	// GetTableBucket does not return tags; fetch them via ListTagsForResource
	// so the delta comparison against Spec.Tags is accurate.
	tagsResp, err := rm.sdkapi.ListTagsForResource(
		ctx,
		&svcsdk.ListTagsForResourceInput{ResourceArn: arn},
	)
	rm.metrics.RecordAPICall("READ_ONE", "ListTagsForResource", err)
	if err != nil {
		return err
	}
	if len(tagsResp.Tags) > 0 {
		ko.Spec.Tags = aws.StringMap(tagsResp.Tags)
	} else {
		ko.Spec.Tags = nil
	}

	// GetTableBucketPolicy returns NotFoundException when no policy is set;
	// treat that as an absent (nil) policy rather than an error.
	polResp, err := rm.sdkapi.GetTableBucketPolicy(
		ctx,
		&svcsdk.GetTableBucketPolicyInput{TableBucketARN: arn},
	)
	rm.metrics.RecordAPICall("READ_ONE", "GetTableBucketPolicy", err)
	if err != nil {
		var notFound *svcsdktypes.NotFoundException
		if !errors.As(err, &notFound) {
			return err
		}
		ko.Spec.Policy = nil
	} else {
		ko.Spec.Policy = polResp.ResourcePolicy
	}

	return nil
}

// customUpdateTableBucket applies the mutable bucket-level configuration via
// the dedicated APIs. TableBucket has no UpdateTableBucket operation; the
// bucket name is immutable. The mutable surface is:
//   - encryption configuration (PutTableBucketEncryption)
//   - storage class configuration (PutTableBucketStorageClass)
//   - tags (TagResource / UntagResource)
func (rm *resourceManager) customUpdateTableBucket(
	ctx context.Context,
	desired *resource,
	latest *resource,
	delta *ackcompare.Delta,
) (updated *resource, err error) {
	rlog := ackrtlog.FromContext(ctx)
	exit := rlog.Trace("rm.customUpdateTableBucket")
	defer func() { exit(err) }()

	// Start from the desired spec, carry over the observed status.
	ko := desired.ko.DeepCopy()
	ko.Status = *latest.ko.Status.DeepCopy()

	arn := arnFromKO(ko)
	if arn == nil {
		// Should not happen: update only runs after a successful create that
		// populates the ARN. Guard defensively to avoid a nil dereference.
		return &resource{ko}, nil
	}

	if delta.DifferentAt("Spec.EncryptionConfiguration") {
		if err := rm.syncEncryption(ctx, desired, arn); err != nil {
			return nil, err
		}
	}
	if delta.DifferentAt("Spec.StorageClassConfiguration") {
		if err := rm.syncStorageClass(ctx, desired, arn); err != nil {
			return nil, err
		}
	}
	if delta.DifferentAt("Spec.Tags") {
		if err := rm.syncTags(ctx, desired, latest, arn); err != nil {
			return nil, err
		}
	}
	if delta.DifferentAt("Spec.Policy") {
		if err := rm.syncPolicy(ctx, desired, arn); err != nil {
			return nil, err
		}
	}

	return &resource{ko}, nil
}

// syncPolicy applies the desired bucket resource policy. An empty/absent policy
// removes it via DeleteTableBucketPolicy; otherwise it is set via
// PutTableBucketPolicy.
func (rm *resourceManager) syncPolicy(
	ctx context.Context,
	desired *resource,
	arn *string,
) (err error) {
	rlog := ackrtlog.FromContext(ctx)
	exit := rlog.Trace("rm.syncPolicy")
	defer func() { exit(err) }()

	if desired.ko.Spec.Policy == nil || *desired.ko.Spec.Policy == "" {
		_, err = rm.sdkapi.DeleteTableBucketPolicy(
			ctx,
			&svcsdk.DeleteTableBucketPolicyInput{TableBucketARN: arn},
		)
		rm.metrics.RecordAPICall("DELETE", "DeleteTableBucketPolicy", err)
		return err
	}

	_, err = rm.sdkapi.PutTableBucketPolicy(
		ctx,
		&svcsdk.PutTableBucketPolicyInput{
			TableBucketARN: arn,
			ResourcePolicy: desired.ko.Spec.Policy,
		},
	)
	rm.metrics.RecordAPICall("UPDATE", "PutTableBucketPolicy", err)
	return err
}

// syncTags reconciles the desired tag set against the latest observed tag set
// using the TagResource and UntagResource APIs.
func (rm *resourceManager) syncTags(
	ctx context.Context,
	desired *resource,
	latest *resource,
	arn *string,
) (err error) {
	rlog := ackrtlog.FromContext(ctx)
	exit := rlog.Trace("rm.syncTags")
	defer func() { exit(err) }()

	from, _ := convertToOrderedACKTags(latest.ko.Spec.Tags)
	to, _ := convertToOrderedACKTags(desired.ko.Spec.Tags)

	added, _, removed := ackcompare.GetTagsDifference(from, to)

	// A key present in both added and removed is a value change; keep it in
	// added (TagResource overwrites) and drop it from removed.
	for key := range removed {
		if _, ok := added[key]; ok {
			delete(removed, key)
		}
	}

	if len(removed) > 0 {
		toRemove := make([]string, 0, len(removed))
		for key := range removed {
			toRemove = append(toRemove, key)
		}
		_, err = rm.sdkapi.UntagResource(
			ctx,
			&svcsdk.UntagResourceInput{
				ResourceArn: arn,
				TagKeys:     toRemove,
			},
		)
		rm.metrics.RecordAPICall("UPDATE", "UntagResource", err)
		if err != nil {
			return err
		}
	}

	if len(added) > 0 {
		toAdd := make(map[string]string, len(added))
		for key, val := range added {
			toAdd[key] = val
		}
		_, err = rm.sdkapi.TagResource(
			ctx,
			&svcsdk.TagResourceInput{
				ResourceArn: arn,
				Tags:        toAdd,
			},
		)
		rm.metrics.RecordAPICall("UPDATE", "TagResource", err)
		if err != nil {
			return err
		}
	}

	return nil
}

// syncEncryption applies the desired encryption configuration to the table
// bucket via PutTableBucketEncryption.
func (rm *resourceManager) syncEncryption(
	ctx context.Context,
	desired *resource,
	arn *string,
) (err error) {
	rlog := ackrtlog.FromContext(ctx)
	exit := rlog.Trace("rm.syncEncryption")
	defer func() { exit(err) }()

	if desired.ko.Spec.EncryptionConfiguration == nil {
		return nil
	}

	encCfg := &svcsdktypes.EncryptionConfiguration{
		KmsKeyArn: desired.ko.Spec.EncryptionConfiguration.KMSKeyARN,
	}
	if desired.ko.Spec.EncryptionConfiguration.SSEAlgorithm != nil {
		encCfg.SseAlgorithm = svcsdktypes.SSEAlgorithm(
			*desired.ko.Spec.EncryptionConfiguration.SSEAlgorithm,
		)
	}

	_, err = rm.sdkapi.PutTableBucketEncryption(
		ctx,
		&svcsdk.PutTableBucketEncryptionInput{
			TableBucketARN:          arn,
			EncryptionConfiguration: encCfg,
		},
	)
	rm.metrics.RecordAPICall("UPDATE", "PutTableBucketEncryption", err)
	return err
}

// syncStorageClass applies the desired storage class configuration to the
// table bucket via PutTableBucketStorageClass.
func (rm *resourceManager) syncStorageClass(
	ctx context.Context,
	desired *resource,
	arn *string,
) (err error) {
	rlog := ackrtlog.FromContext(ctx)
	exit := rlog.Trace("rm.syncStorageClass")
	defer func() { exit(err) }()

	if desired.ko.Spec.StorageClassConfiguration == nil ||
		desired.ko.Spec.StorageClassConfiguration.StorageClass == nil {
		return nil
	}

	_, err = rm.sdkapi.PutTableBucketStorageClass(
		ctx,
		&svcsdk.PutTableBucketStorageClassInput{
			TableBucketARN: arn,
			StorageClassConfiguration: &svcsdktypes.StorageClassConfiguration{
				StorageClass: svcsdktypes.StorageClass(
					*desired.ko.Spec.StorageClassConfiguration.StorageClass,
				),
			},
		},
	)
	rm.metrics.RecordAPICall("UPDATE", "PutTableBucketStorageClass", err)
	return err
}

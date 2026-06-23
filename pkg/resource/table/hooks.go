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

package table

import (
	"context"

	ackcompare "github.com/aws-controllers-k8s/runtime/pkg/compare"
	ackrtlog "github.com/aws-controllers-k8s/runtime/pkg/runtime/log"
	svcapitypes "github.com/aws-controllers-k8s/s3tables-controller/apis/v1alpha1"
	"github.com/aws/aws-sdk-go-v2/aws"
	svcsdk "github.com/aws/aws-sdk-go-v2/service/s3tables"
)

// tableARNFromStatus returns the Table's ARN from the common
// ACKResourceMetadata, used as the ResourceArn for the Tag/Untag/ListTags APIs.
func tableARNFromStatus(ko *svcapitypes.Table) *string {
	if ko.Status.ACKResourceMetadata != nil && ko.Status.ACKResourceMetadata.ARN != nil {
		return (*string)(ko.Status.ACKResourceMetadata.ARN)
	}
	return nil
}

// setResourceTags augments the Table spec with the resource tags, which are not
// returned by GetTable. They have a dedicated ListTagsForResource API. Reading
// them here keeps observed state consistent with desired state and prevents
// spurious deltas on every reconciliation.
func (rm *resourceManager) setResourceTags(
	ctx context.Context,
	ko *svcapitypes.Table,
) (err error) {
	rlog := ackrtlog.FromContext(ctx)
	exit := rlog.Trace("rm.setResourceTags")
	defer func() { exit(err) }()

	arn := tableARNFromStatus(ko)
	if arn == nil {
		return nil
	}

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

	return nil
}

// customUpdateTable reconciles the mutable surface of a Table. There is no
// generic UpdateTable API; the mutable aspects are:
//   - the table name -> RenameTable (newName), guarded by the version token
//   - tags -> TagResource / UntagResource
//
// All other fields (namespace, format, metadata, encryption, storage class)
// are create-only/immutable.
func (rm *resourceManager) customUpdateTable(
	ctx context.Context,
	desired *resource,
	latest *resource,
	delta *ackcompare.Delta,
) (updated *resource, err error) {
	rlog := ackrtlog.FromContext(ctx)
	exit := rlog.Trace("rm.customUpdateTable")
	defer func() { exit(err) }()

	// Start from desired spec, carry over observed status.
	ko := desired.ko.DeepCopy()
	ko.Status = *latest.ko.Status.DeepCopy()

	if delta.DifferentAt("Spec.Name") {
		if err := rm.renameTable(ctx, desired, latest); err != nil {
			return nil, err
		}
	}

	if delta.DifferentAt("Spec.MetadataLocation") {
		if err := rm.updateMetadataLocation(ctx, desired, latest); err != nil {
			return nil, err
		}
	}

	if delta.DifferentAt("Spec.Tags") {
		arn := tableARNFromStatus(ko)
		if arn != nil {
			if err := rm.syncTags(ctx, desired, latest, arn); err != nil {
				return nil, err
			}
		}
	}

	return &resource{ko}, nil
}

// renameTable changes the table name via RenameTable. It targets the CURRENT
// (latest) bucket/namespace/name and supplies the desired new name, guarded by
// the latest observed version token.
func (rm *resourceManager) renameTable(
	ctx context.Context,
	desired *resource,
	latest *resource,
) (err error) {
	rlog := ackrtlog.FromContext(ctx)
	exit := rlog.Trace("rm.renameTable")
	defer func() { exit(err) }()

	input := &svcsdk.RenameTableInput{
		TableBucketARN: latest.ko.Spec.TableBucketARN,
		Namespace:      latest.ko.Spec.Namespace,
		Name:           latest.ko.Spec.Name,
		NewName:        desired.ko.Spec.Name,
	}
	if latest.ko.Status.VersionToken != nil {
		input.VersionToken = latest.ko.Status.VersionToken
	}

	_, err = rm.sdkapi.RenameTable(ctx, input)
	rm.metrics.RecordAPICall("UPDATE", "RenameTable", err)
	return err
}

// updateMetadataLocation applies the desired Iceberg metadata location via the
// dedicated UpdateTableMetadataLocation API. metadataLocation is not part of
// CreateTable; it can only be changed through this operation, guarded by the
// latest observed version token.
func (rm *resourceManager) updateMetadataLocation(
	ctx context.Context,
	desired *resource,
	latest *resource,
) (err error) {
	rlog := ackrtlog.FromContext(ctx)
	exit := rlog.Trace("rm.updateMetadataLocation")
	defer func() { exit(err) }()

	if desired.ko.Spec.MetadataLocation == nil {
		return nil
	}

	input := &svcsdk.UpdateTableMetadataLocationInput{
		TableBucketARN:   latest.ko.Spec.TableBucketARN,
		Namespace:        latest.ko.Spec.Namespace,
		Name:             latest.ko.Spec.Name,
		MetadataLocation: desired.ko.Spec.MetadataLocation,
	}
	if latest.ko.Status.VersionToken != nil {
		input.VersionToken = latest.ko.Status.VersionToken
	}

	_, err = rm.sdkapi.UpdateTableMetadataLocation(ctx, input)
	rm.metrics.RecordAPICall("UPDATE", "UpdateTableMetadataLocation", err)
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

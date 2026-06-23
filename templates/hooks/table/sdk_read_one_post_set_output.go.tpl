	// GetTable returns `namespace` as a list; bridge the first element back into
	// the scalar Spec.Namespace so observed state matches desired.
	if len(resp.Namespace) > 0 {
		ko.Spec.Namespace = &resp.Namespace[0]
	}
	// The generated output mapping skips NamespaceId/TableBucketId: the
	// code-generator output loop matches SDK member names (`NamespaceId`,
	// `TableBucketId`) against the resource field keys, which are stored with
	// ACK's `ID` initialism (`NamespaceID`, `TableBucketID`), so the members
	// don't match and are skipped. (Unrelated to the `namespace` list field
	// dropped via ignore.field_paths — see TableBucket, which hits the same skip
	// with no ignore rule.) Populate those read-only status fields here.
	if resp.NamespaceId != nil {
		ko.Status.NamespaceID = resp.NamespaceId
	}
	if resp.TableBucketId != nil {
		ko.Status.TableBucketID = resp.TableBucketId
	}
	// GetTable does not return tags; fetch them via ListTagsForResource so the
	// delta against Spec.Tags is accurate and we avoid spurious deltas.
	if err := rm.setResourceTags(ctx, ko); err != nil {
		return nil, err
	}

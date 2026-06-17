	// The generated SetResource output mapping does not emit assignments for
	// NamespaceId/TableBucketId. The code-generator output loop matches SDK
	// member names (`NamespaceId`, `TableBucketId`) against the resource field
	// keys, which are stored with ACK's `ID` initialism (`NamespaceID`,
	// `TableBucketID`); the names don't match, so the members are skipped. (This
	// is unrelated to the `namespace` list field dropped via ignore.field_paths
	// — see TableBucket, which hits the same skip with no ignore rule.) Populate
	// these AWS-assigned read-only status fields here so they reach the CR.
	if resp.NamespaceId != nil {
		ko.Status.NamespaceID = resp.NamespaceId
	}
	if resp.TableBucketId != nil {
		ko.Status.TableBucketID = resp.TableBucketId
	}

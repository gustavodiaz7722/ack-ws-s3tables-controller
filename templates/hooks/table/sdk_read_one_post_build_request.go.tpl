	// Read the table by its stable ARN rather than the name triplet. GetTable
	// accepts the triplet (tableBucketARN + namespace + name) OR tableArn, but
	// not both. Keying off the ARN lets GetTable return the CURRENT AWS name, so
	// a change to spec.name surfaces as a delta and drives RenameTable. Falls
	// back to the name triplet before the ARN is known (first reconcile).
	if r.ko.Status.ACKResourceMetadata != nil && r.ko.Status.ACKResourceMetadata.ARN != nil {
		input.TableArn = (*string)(r.ko.Status.ACKResourceMetadata.ARN)
		input.TableBucketARN = nil
		input.Namespace = nil
		input.Name = nil
	} else {
		input.TableArn = nil
	}
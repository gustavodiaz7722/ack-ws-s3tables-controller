	// metadataLocation is not part of CreateTable; it can only be applied via
	// the dedicated UpdateTableMetadataLocation API. When the user supplies a
	// desired metadataLocation, requeue so the next reconciliation observes the
	// delta and customUpdateTable syncs it via UpdateTableMetadataLocation.
	if desired.ko.Spec.MetadataLocation != nil {
		ackcondition.SetSynced(&resource{ko}, corev1.ConditionFalse, aws.String("table created, requeue for updates"), nil)
		err = ackrequeue.NeededAfter(fmt.Errorf("Reconciling to sync additional fields"), time.Second)
		return &resource{ko}, err
	}

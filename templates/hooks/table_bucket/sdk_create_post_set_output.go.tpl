	// TableBucket sub-resources (resource policy, maintenance configuration)
	// are not part of CreateTableBucket; they are applied by
	// customUpdateTableBucket via their dedicated Put APIs. Without a nudge the
	// resource would be marked Synced=True right after create and those fields
	// would not be applied until the next periodic resync. When either is
	// declared in the spec, mark the resource not-yet-synced so the runtime
	// requeues immediately and the update reconcile applies them.
	if desired.ko.Spec.Policy != nil || desired.ko.Spec.MaintenanceConfiguration != nil {
		msg := "sub-resource update pending; resource will be requeued"
		ackcondition.SetSynced(&resource{ko}, corev1.ConditionFalse, &msg, nil)
	}

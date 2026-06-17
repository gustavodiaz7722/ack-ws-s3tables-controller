	// The SDK CreateNamespace input models `namespace` as a list, but the CR
	// exposes a single scalar Name (the namespace is always a single element).
	// Bridge the scalar Spec.Name into the single-element list the API expects.
	if desired.ko.Spec.Name != nil {
		input.Namespace = []string{*desired.ko.Spec.Name}
	}

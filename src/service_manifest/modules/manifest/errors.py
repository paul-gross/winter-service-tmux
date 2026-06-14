class ManifestError(Exception):
    """Raised when a service manifest cannot be read or is structurally invalid.

    Callers that want to handle manifest failures specifically (e.g. the doctor
    probe, a future orchestrator) catch this type rather than importing the
    underlying tomllib or IO exceptions. That is the YAGNI test: ManifestError
    earns its keep because those callers would otherwise have to depend on
    tomllib / OSError specifics.
    """

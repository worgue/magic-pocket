"""Provider provisioning helpers exposed as importable runtime APIs.

These let an external provisioner (which creates the backend and bakes the
connection URL into SSM / Secrets Manager) reuse pocket's own ensure + URL
derivation instead of re-implementing it and drifting from deploy. See
:mod:`pocket.provisioning.neon`.
"""

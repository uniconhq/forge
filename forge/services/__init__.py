"""The services: one function per thing the platform does, composing the
database half with the forge half through the port. The functions the
backend calls are actions, marked `@action` from `forge.actions`; the rest
are building blocks the actions call. The only layer that writes to the
database. Never imports `forge.forges`.
"""

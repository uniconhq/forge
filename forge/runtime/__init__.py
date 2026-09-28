"""How a call runs in a process: the `Context` a service function runs with,
the `@action` mark that opens a unit of work around it, the setup the process
holds, the `Setup` that owns the pool and the forge, and the loops that run
with nobody clicking. The only part of the package besides `forge.testing`
that builds a forge implementation.
"""

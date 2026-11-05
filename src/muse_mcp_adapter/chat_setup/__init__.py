"""Chat-driven setup: define servers, tools and policies by prompt, confirmed by the owner.

The agent can look things up, open proposals and narrow access through a built-in connector. A
proposal takes effect only when the owner approves it on a page of this service, outside the
model. Import only :mod:`.models` from code that the HTTP application package loads first; the
other modules depend on that package.
"""

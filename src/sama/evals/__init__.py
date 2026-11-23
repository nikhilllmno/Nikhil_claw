"""Small evals that run real queries against a sama API (docs/architecture.md, Section 18.4).

An eval is not a unit test: it calls a deployed sama with a real user token, spends model and
browser money, and reports counts. Run it by hand against staging, before and after a change.
"""

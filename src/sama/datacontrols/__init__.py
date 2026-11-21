"""Data controls: export, delete, and retention per org and per user (docs/architecture.md,
Section 17.4).

- ``sama.datacontrols.export``: the ZIP export of an org or a user;
- ``sama.datacontrols.delete``: the deletes, in one transaction each;
- ``sama.datacontrols.purge``: the cleanups outside Postgres after a delete;
- ``sama.datacontrols.retention``: the retention rule of each plan;
- ``sama.datacontrols.sweep``: the retention sweep;
- ``sama.datacontrols.service``: the service that the API calls.
"""

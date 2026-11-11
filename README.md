# CloudRecover

CloudRecover backs up configured databases and website files to Amazon S3 and keeps backup history per client profile. The central administrator can review activity across all clients. Each client login is limited to its own profile and history.

## Quick start

1. Install backend dependencies with `python -m pip install -r backend/requirements.txt`.
2. Copy `backend/.env.example` to `backend/.env` and configure the encryption key, a persistent `CLOUDRECOVER_SESSION_SECRET`, and AWS access. The prototype administrator password defaults to `ca$123`.
3. Start the API from the `backend` directory with `python -m uvicorn main:app --host 127.0.0.1 --port 8000`.
4. Sign in as `admin`. Client IDs are their registered names; initial passwords are `<client-name>$123`.

From the administration dashboard, add either a website profile or a local Docker MySQL profile. Docker profiles need the container name, database name, MySQL user, and that user's password configured as `DB_PASSWORD` for the backup engine.

Client passwords follow the requested prototype convention and are predictable. Use a proper credential store and HTTPS before making this service publicly available. See [backend setup and connector details](backend/README.md).

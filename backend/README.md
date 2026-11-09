# CloudRecover client and MongoDB setup

The dashboard now supports multiple client profiles in this local administration prototype. Each profile can contain a website URL, an optional website-files source over SFTP, a MongoDB database connection, and its own automated schedule and backup history. SFTP files and MongoDB archives are stored as separate S3 objects under `cloudrecover/backups/clients/<client-id>/`.

## Setup

1. Install Python dependencies in the backend environment:

   ```powershell
   cd C:\Users\Lenovo\Desktop\ccbdi_project\CloudRecover\backend
   .\venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

2. Install MongoDB Database Tools on the CloudRecover host and ensure `mongodump` is on `PATH`. Set `MONGODUMP_BIN` in `backend/.env` if the executable is elsewhere. `mongodump` creates a gzip-compressed archive and accepts sensitive connection URIs through a config file; CloudRecover uses that option rather than putting the URI in the process arguments. See [MongoDB Database Tools installation](https://www.mongodb.com/docs/database-tools/installation/) and [`mongodump` options](https://www.mongodb.com/docs/database-tools/mongodump/).

3. Copy `.env.example` to `backend/.env`, generate a Fernet key once, and retain the same key securely. For example:

   ```powershell
   .\venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Put that value in `CLOUDRECOVER_ENCRYPTION_KEY` in `backend/.env`. Losing or changing the key makes saved MongoDB URIs unreadable. Do not commit either `.env` file.

4. Make sure the backend process can access AWS credentials for the configured S3 bucket. For MongoDB Atlas or a hosted MongoDB deployment, allow the CloudRecover host's outbound IP and use a database user with only the required read/backup privileges.

5. For website files, configure SFTP in the client form and install the server's SSH host key into the CloudRecover user's `known_hosts`. Use a dedicated read-only SFTP account and key.

6. Configure login signing and administrator credentials in `backend/.env` before starting the service:

   ```dotenv
   CLOUDRECOVER_SESSION_SECRET=<a long random secret>
   CLOUDRECOVER_ADMIN_PASSWORD=ca$123
   CLOUDRECOVER_COOKIE_SECURE=true
   ```

   Generate a session secret with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Use HTTPS when `CLOUDRECOVER_COOKIE_SECURE=true`. The administrator login ID is `admin` (also accepted: `central administration`, `central-admin`, or `ca`). Each client's login ID is its registered client name, and its initial password is `<client-name>$123` (case insensitive for the ID). These initial passwords are shared defaults; replace them with unique credentials before exposing the service publicly.

7. Run the dashboard:

   ```powershell
   .\venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
   ```

Select **Add website**, enter a client/site name, choose MongoDB, enter its database name and connection URI, and optionally configure SFTP file access. **Back Up Now** runs each configured source for the selected client. Automated schedules are per client and run only while the API process is running.

## Current boundaries

- Central administration can inspect all registered clients and cross-client backup activity. Client accounts are bound to one registered client, and all API routes enforce that scope server side. Recovery controls and client management are administrator only.
- MongoDB backups require `mongodump` on the CloudRecover host and network access from that host to MongoDB. They are uploaded with S3 server-side encryption and a SHA-256 sidecar.
- Website file backup currently uses SFTP. A website database can be backed up through its MongoDB URI; provider-specific website databases such as managed MySQL/PostgreSQL need additional connectors.
- Restore creates a separate local MySQL 8.4 Docker container and volume, verifies the backup checksum, and imports the SQL dump. Each restore gets a generated root password saved in `cloudrecover.json` and shown in Recovery activity; keep that file private. Connect with `docker exec -it <container> mysql -uroot -p` (then enter the displayed password). MongoDB restore and deploying website files back to a hosting provider are not implemented yet.
- SFTP and MongoDB are recorded as separate artifacts/history entries. If one source fails, a successful artifact from the other source may still exist.

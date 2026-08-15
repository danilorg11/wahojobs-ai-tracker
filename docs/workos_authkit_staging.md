# WorkOS AuthKit local Staging rehearsal

This activation is deliberately local and explicit. It composes the accepted
AuthKit, Accounts, PB-OWN-1, B2D1, B2C4, profile, matching, logout, browser,
database-lifetime, HTTP-adapter, and ephemeral-TLS boundaries. It does not add
another authentication flow and never migrates a database during startup.

The WorkOS dashboard environment is Staging. The corresponding WahoJobs
environment namespace is `private_beta`, which is the existing B2D1-supported
non-production namespace.

## External configuration

Use this one external secret-bearing file on Windows:

```text
%LOCALAPPDATA%\WahoJobs\authkit-staging.json
```

For the ordinary Local user in this checkout, that resolves to:

```text
C:\Users\danrg\AppData\Local\WahoJobs\authkit-staging.json
```

The launcher accepts only an explicit absolute `--config` path. The file and
database must already exist outside every Git checkout and may not be a symlink,
reparse point, or multiply linked file. There is no environment-variable,
workspace-database, or default fallback.

Create a private directory and placeholder document without putting a secret on
the command line:

```powershell
$configDirectory = Join-Path $env:LOCALAPPDATA 'WahoJobs'
$configPath = Join-Path $configDirectory 'authkit-staging.json'
$userSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
New-Item -ItemType Directory -Force -Path $configDirectory | Out-Null
icacls $configDirectory /inheritance:r /grant:r "*$($userSid):(OI)(CI)F" "*S-1-5-18:(OI)(CI)F"
$template = @'
{
  "version": 1,
  "environment_namespace": "private_beta",
  "database_path": "C:\\ABSOLUTE\\EXTERNAL\\PATH\\wahojobs-private-beta.sqlite3",
  "public_origin": "https://127.0.0.1:8443",
  "redirect_uri": "https://127.0.0.1:8443/auth/workos/callback",
  "workos_client_id": "client_REPLACE_WITH_STAGING_CLIENT_ID",
  "workos_api_key": "REPLACE_FROM_PASSWORD_MANAGER",
  "wahojobs_invitation_lookup_key_base64": "REPLACE_WITH_BASE64_OF_EXISTING_RAW_INVITATION_KEY",
  "session_idle_ttl_seconds": 3600,
  "session_absolute_ttl_seconds": 28800
}
'@
[System.IO.File]::WriteAllText($configPath, $template, [System.Text.UTF8Encoding]::new($false))
icacls $configPath /inheritance:r /grant:r "*$($userSid):F" "*S-1-5-18:F"
notepad.exe $configPath
```

Fill the WorkOS Client ID, WorkOS API key, invitation key, and explicit database
path manually. The API key and invitation lookup key are secrets. The Client ID,
database path, origins, namespace, and TTLs are nonsecret configuration (although
the database contents remain sensitive).

The invitation lookup key field is canonical standard Base64 of the existing raw
M002 invitation HMAC key. Copy that value to the clipboard without printing it:

```powershell
$invitationKeyPath = Read-Host 'Absolute path to the existing raw invitation lookup key file'
[Convert]::ToBase64String([IO.File]::ReadAllBytes($invitationKeyPath)) | Set-Clipboard
```

Paste it into the JSON field, save, and clear the clipboard after use:

```powershell
Set-Clipboard -Value $null
```

## M008 prerequisite

Startup opens only the explicit database in existing-file mode, acquires the
existing durable-runtime lifetime ownership, rejects SQLite sidecars, requires a
writable connection, and validates exact M008, closed-schema and Accounts
attestation, quick integrity, and foreign keys. It never initializes, repairs, or
migrates the database.

If the authorized external database is still exact M007, stop every runtime and
apply the already accepted M008 migration explicitly under offline-operator
ownership:

```powershell
python -B scripts/workos_authkit_staging_migrate.py --database "C:\ABSOLUTE\EXTERNAL\PATH\wahojobs-private-beta.sqlite3"
```

The command is idempotent for exact M008 and fails closed for any other schema.

## Start and stop

Install the hash-locked Python 3.12 dependencies in the intended environment if
they are not already installed:

```powershell
python -m pip install --require-hashes -r requirements.lock
```

Start the rehearsal only after filling and permission-restricting the external
file:

```powershell
python -B scripts/workos_authkit_staging_app.py --config "$env:LOCALAPPDATA\WahoJobs\authkit-staging.json"
```

The launcher binds only `127.0.0.1:8443`, creates the existing ephemeral
self-signed local certificate outside the repository, and routes requests only
through `WorkOSAuthKitBrowserIntegration`. The certificate covers `127.0.0.1`
and `localhost`; the browser may require explicit acceptance for this local
rehearsal. A port conflict, TLS failure, invalid secret/configuration, unavailable
database, or non-M008 schema fails before serving.

Press Ctrl+C to stop. The listener waits for request threads to finish, closes the
AuthKit browser/profile/gateway composition, clears pending process-local login
transactions, and releases database lifetime ownership.


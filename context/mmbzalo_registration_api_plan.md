# MMBZalo Registration API Planning

## Current State

The deployed API currently exposes authentication and workspace access for existing users only:

```text
POST /api/auth/login
POST /api/auth/logout
GET  /api/auth/me
GET  /api/workspaces
POST /api/workspaces/{workspace_id}/switch
GET  /api/workspace-session
```

There is currently no public registration, invitation, password reset, or workspace creation API visible from OpenAPI. This means new users cannot create accounts from the public site yet. They can only log in if a user record already exists in the database.

The current admin account can:

```text
log in
access Main Workspace
use the saved Zalo browser profile
sync contacts
send messages
view jobs/history
```

## Product Rule

Each independent user or business should usually get their own workspace.

A workspace maps to:

```text
workspace
→ Zalo browser session
→ Chrome profile path
→ contacts
→ jobs
→ sends/campaigns
```

The intended isolation model is:

```text
User A → Workspace A → Zalo QR/session A → contacts A
User B → Workspace B → Zalo QR/session B → contacts B
```

Do not put unrelated users in the same workspace, because they would share:

```text
Zalo login session
contact list
job history
send automation
campaigns
browser profile
```

Multiple users should share one workspace only when they are part of the same company/team and intentionally operate the same Zalo account.

Example:

```text
Company X
├── Owner
├── Staff 1
└── Staff 2

All share Workspace X
```

Suggested role meanings:

```text
owner/admin: manage workspace, users, Zalo login, automation settings
operator/member: view contacts and send messages
viewer: read-only access
```

## Goal

Add a safe registration and onboarding flow so a new user can:

```text
sign up
→ get a default workspace
→ log in
→ scan their own Zalo QR
→ sync contacts
→ send messages
```

## Minimum Backend APIs

### 1. Register User

```text
POST /api/auth/register
```

Purpose:

```text
create user
hash password
create default workspace
add user as workspace owner/admin
create empty workspace session row
return authenticated session or login-ready response
```

Example request:

```json
{
  "email": "user@example.com",
  "password": "strong-password",
  "display_name": "User Name",
  "workspace_name": "User Workspace"
}
```

Example response:

```json
{
  "user": {
    "user_id": "uuid",
    "email": "user@example.com",
    "display_name": "User Name",
    "is_platform_admin": false
  },
  "workspace": {
    "workspace_id": "uuid",
    "name": "User Workspace",
    "slug": "user-workspace",
    "role": "owner"
  }
}
```

Security requirements:

```text
normalize email to lowercase
enforce unique email
hash password using the same method as login
validate password strength
do not expose password hash
rate-limit registration
return generic errors for email enumeration risk
```

### 2. Create Workspace

```text
POST /api/workspaces
```

Purpose:

```text
allow an authenticated user to create another workspace
create workspace
add creator as owner/admin
create workspace_session row
```

Example request:

```json
{
  "name": "New Workspace"
}
```

Example response:

```json
{
  "workspace_id": "uuid",
  "name": "New Workspace",
  "slug": "new-workspace",
  "role": "owner"
}
```

### 3. Workspace Members List

```text
GET /api/workspaces/{workspace_id}/members
```

Purpose:

```text
show current members of a workspace
restricted to owner/admin
```

Example response:

```json
{
  "members": [
    {
      "user_id": "uuid",
      "email": "owner@example.com",
      "display_name": "Owner",
      "role": "owner"
    }
  ]
}
```

### 4. Invite Workspace Member

```text
POST /api/workspaces/{workspace_id}/members/invite
```

Purpose:

```text
invite an existing or new user to a workspace
assign role
optionally send invitation email later
```

Initial implementation can be simple:

```text
if user exists: add membership
if user does not exist: create pending invitation
```

Later implementation:

```text
send invitation email
tokenized invite link
invite expiration
accept invite endpoint
```

### 5. Remove Workspace Member

```text
DELETE /api/workspaces/{workspace_id}/members/{user_id}
```

Purpose:

```text
remove a user from a workspace
owner/admin only
prevent removing the last owner
```

### 6. Accept Invite

```text
POST /api/invitations/{token}/accept
```

Purpose:

```text
allow invited user to create password or join workspace
```

This can be delayed if invitation emails are not in scope yet.

## Recommended Database Model

Use existing tables where available. If the current schema already has these equivalents, adapt names rather than duplicating.

### users

Fields likely needed:

```text
id uuid primary key
email text unique not null
password_hash text not null
display_name text
is_platform_admin boolean default false
created_at timestamptz
updated_at timestamptz
```

### workspaces

Fields likely needed:

```text
id uuid primary key
name text not null
slug text unique or workspace-scoped unique
created_by_user_id uuid references users(id)
created_at timestamptz
updated_at timestamptz
```

### workspace_members

Fields likely needed:

```text
workspace_id uuid references workspaces(id)
user_id uuid references users(id)
role text not null
created_at timestamptz

unique(workspace_id, user_id)
```

Recommended roles:

```text
owner
admin
operator
viewer
```

### workspace_sessions

Each workspace should have one Zalo session state row:

```text
workspace_id uuid primary key references workspaces(id)
owner_worker_id text
login_state text
profile_path text
profile_name text
profile_avatar_url text
phone_number text
last_authenticated_at timestamptz
last_validated_at timestamptz
error_message text
```

Profile path should be derived from workspace ID:

```text
/var/lib/app/browser-profiles/<workspace-id>
```

## Registration Flow

Recommended first version:

```text
1. User visits https://linhanhchip.site
2. User clicks Sign Up
3. User enters email, password, display name, workspace name
4. Backend creates user
5. Backend creates default workspace
6. Backend adds user as owner
7. Backend creates empty workspace session row
8. User logs in or receives session immediately
9. User clicks Start Login
10. User scans Zalo QR for that workspace
11. App stores authenticated Zalo profile in /var/lib/app/browser-profiles/<workspace-id>
12. User clicks Sync Contacts
13. Contacts sync into that workspace only
```

## Frontend Changes

Add a registration screen or mode beside the current login form.

Required screens/components:

```text
Sign In
Sign Up
Workspace switcher
Workspace creation
Workspace members/settings
Invite member
Zalo session onboarding
```

Minimum frontend for registration:

```text
email input
password input
display name input
workspace name input
submit button
error display
redirect to logged-in dashboard after success
```

After first login, show onboarding state:

```text
No Zalo session yet
→ Start Login
→ QR scan
→ Sync Contacts
```

## API Permission Rules

Suggested rules:

```text
/api/auth/register: public, rate-limited
/api/auth/login: public, rate-limited
/api/auth/me: authenticated
/api/workspaces: authenticated, list only user's workspaces
/api/workspaces POST: authenticated
/api/workspaces/{id}/switch: authenticated and member of workspace
/api/workspace-session: authenticated and active workspace member
contact sync/send/campaign routes: authenticated and active workspace member
workspace member management: owner/admin only
```

## Safety Requirements

Before public signup:

```text
rotate exposed database password and app secrets
enforce HTTPS-only cookies
use secure cookie domain linhanhchip.site
rate-limit login/register
add CSRF protection if cookie-based auth is used for browser forms
validate CORS allows only https://linhanhchip.site
prevent cross-workspace data leakage
prevent non-members from accessing workspace data
prevent two browser processes from using the same workspace profile concurrently
```

Current production `.env` should use:

```env
MMBZALO_COOKIE_SECURE=true
MMBZALO_COOKIE_DOMAIN=linhanhchip.site
MMBZALO_CORS_ALLOWED_ORIGINS='["https://linhanhchip.site"]'
```

## Manual Test Plan

### Registration

```text
POST /api/auth/register
→ returns user and workspace
→ user can log in
→ /api/auth/me returns new user
→ /api/workspaces returns exactly the new workspace
```

### Workspace Isolation

```text
User A cannot see User B workspace
User A cannot access User B contacts
User A cannot access User B jobs
User A cannot switch to User B workspace
```

### Zalo QR Onboarding

```text
New user logs in
→ workspace session is idle/not connected
→ Start Login opens QR flow
→ user scans QR
→ workspace_session becomes authenticated
→ profile_path = /var/lib/app/browser-profiles/<new-workspace-id>
```

### Contact Sync

```text
New user syncs contacts
→ contact_sync job succeeds
→ contacts appear only under that workspace
→ database contacts count for that workspace > 0
```

### Send Message

```text
New user sends one test message
→ manual_send job succeeds
→ job events persisted
→ no duplicate sends
```

## Implementation Order

Recommended order:

```text
1. Confirm current user/workspace/member schema.
2. Add POST /api/auth/register.
3. Add automatic default workspace creation.
4. Add workspace_session creation for new workspace.
5. Add frontend Sign Up form.
6. Add workspace creation API if needed.
7. Add workspace membership/invite APIs.
8. Add permission checks for every workspace-scoped endpoint.
9. Add E2E tests for registration → QR → sync → send.
10. Invite limited beta users.
```

## Open Questions

```text
Should registration be open to anyone or invite-only?
Should each user get exactly one default workspace at signup?
Should users be allowed to create multiple workspaces?
What roles are needed at launch: owner/admin/operator/viewer?
Will password reset be required before beta?
Will email verification be required before creating a workspace?
Will QR login be displayed directly in frontend or still depend on server-side browser streaming?
```

## Recommended MVP Decision

For the first production-ready version:

```text
use invite-only or admin-created accounts if risk is high
otherwise implement public POST /api/auth/register
create exactly one default workspace per new account
make the new user workspace owner
require each workspace to scan its own Zalo QR
do not share the admin workspace with unrelated users
```

# MMBZalo Frontend Polish Plan

## Objective

Polish the existing frontend without changing working backend behavior.

Keep:

```text
Dark visual style
Existing APIs
Registration and workspace flows
Zalo QR connection flow
Contact sync
Manual messaging
Campaign creation and execution
```

Improve:

```text
Onboarding clarity
Campaign usability
Progress presentation
Notifications
Loading/error states
Spacing, hierarchy, and responsiveness
```

---

## 1. Account Tab: Guided Onboarding

Redesign the Account tab as a clear onboarding flow.

### Step states

```text
1. Account ready
2. Workspace ready
3. Zalo connection
4. Contact sync
5. Ready to message
```

Show each step as completed, active, or pending.

### Zalo session card

Render by state:

```text
idle
→ Show Connect Zalo button

waiting_qr + qr_image_base64
→ Show large centered QR
→ Show scan instructions
→ Show Stop and Refresh QR actions

waiting_qr + no QR yet
→ Show Preparing QR
→ Continue polling

authenticated
→ Show Connected badge
→ Show last authenticated time
→ Make Sync Contacts the primary next action

error
→ Show readable error
→ Show Retry and Stop buttons
```

### Contact sync

After authentication:

```text
Show Sync Contacts prominently
Show queued/running/succeeded/failed state
Show result count
Show last sync time
Show Retry on failure
```

### Activity log

Replace raw technical output with user-facing events:

```text
Zalo connected
Contact sync started
212 contacts synced
Message sent
Campaign completed
```

Keep raw logs inside a collapsed “Technical details” section.

---

## 2. Campaign Section: Simplify Recipient Selection

### Remove redundant search field

Remove the standalone `Search Contacts` input from the campaign form.

Reason:

```text
It overlaps with Preview Matches
Choose From Contacts already opens a filtered contact picker
It creates two competing recipient-selection workflows
```

### Primary recipient flow

Use one clear flow:

```text
Choose From Contacts
→ Open modal/dialog
→ Filter by name and available contact attributes
→ Select contacts
→ Confirm selection
→ Return selected contacts to campaign form
```

The contact picker should support:

```text
Search by name
Optional filters
Select all visible
Clear selection
Selected count
Confirm selection
```

### Remove or reduce Preview Matches

Preferred behavior:

```text
Remove Preview Matches if it only mirrors contact-picker filtering
```

If backend compatibility requires it:

```text
Keep it as a secondary advanced action
Hide it under “Advanced recipient filters”
```

### Selected recipients panel

Show a compact summary:

```text
5 recipients selected
A Duy
Nguyễn Huy Long
Nhật
+2 more
```

Actions:

```text
Edit selection
Clear selection
```

Do not show a large empty panel before recipients are selected.

---

## 3. Campaign Form Layout

Recommended order:

```text
Campaign name
Selected recipients
Message
Send delay
Save Campaign
Save + Execute
```

Move message and delay fields above execution results.

Use progressive disclosure:

```text
Basic fields visible by default
Advanced filters/settings collapsed
```

Buttons:

```text
Save Campaign → secondary
Save + Execute → primary
```

Disable execution when:

```text
No recipients selected
Message is empty
Campaign is running
```

Add clear validation messages near the affected fields.

---

## 4. Campaign Progress: Compact and Scrollable

The current progress log consumes too much vertical space.

### Replace with summary-first layout

Top summary:

```text
Status: Completed
Progress: 5/5
Sent: 5
Failed: 0
Duration: 43s
```

Add a progress bar.

### Event log

Put detailed campaign events inside a bounded container:

```text
Maximum height
Vertical overflow scrolling
Newest events first or auto-scroll to latest
```

Suggested behavior:

```text
Show only the latest 5 events initially
“View all activity” expands the log
Raw worker/job events remain hidden under Technical details
```

### Recipient results

Use a compact result list:

```text
A Duy — Sent
Nguyễn Huy Long — Sent
Nhật — Sent
```

Show route metadata only when expanded:

```text
Direct thread
Search fallback
Not found
Duration
```

Do not repeat the same event in both progress and final result panels.

---

## 5. Recent Campaigns

Use a compact list or table with:

```text
Campaign name
Status
Recipients
Sent/failed
Created time
Actions
```

Actions:

```text
Load
Duplicate
View result
```

Visually distinguish:

```text
Draft
Running
Completed
Failed
Cancelled
```

Avoid showing multiple campaigns with identical names without timestamps or IDs that help distinguish them.

---

## 6. Notification System

Add a global in-app notification/toast component.

### Notification types

```text
Success
Error
Warning
Information
Progress
```

Examples:

```text
Zalo connected successfully
Contact sync completed: 212 contacts
Campaign completed: 5 sent, 0 failed
Message failed to send
QR expired — refresh and try again
```

### Behavior

```text
Toast appears in top-right or bottom-right
Auto-dismiss success/info
Keep errors visible longer
Allow manual close
Prevent duplicate repeated toasts
```

For long-running jobs:

```text
Show “Campaign started”
Update when completed or failed
```

Optional later enhancement:

```text
Browser Notification API after explicit user permission
Use only when tab is hidden or user is away
```

Do not require browser push notifications for the MVP; in-app notifications are required.

---

## 7. Shared Loading and Error UX

For every API action:

```text
Disable button while request is running
Show spinner or progress state
Prevent duplicate submissions
Show readable success/error feedback
Provide Retry when appropriate
```

Examples:

```text
Connecting Zalo...
Preparing QR...
Syncing contacts...
Sending campaign 3/5...
```

Replace raw backend errors with user-facing summaries. Keep full error data in Technical details.

---

## 8. Visual and Responsive Polish

Keep the current dark theme but improve:

```text
Section spacing
Typography hierarchy
Card density
Button hierarchy
Form alignment
Mobile responsiveness
Empty states
Status badges
```

Campaign page should avoid becoming one extremely long page.

Use:

```text
Tabs
Collapsible sections
Modals
Bounded scroll areas
Sticky action bar where useful
```

On mobile:

```text
Stack form columns
Keep QR readable
Make dialogs full-width
Keep primary actions reachable
Avoid horizontal scrolling
```

---

## 9. Constraints

```text
Do not change backend APIs unless strictly required
Preserve all current working flows
Do not break registration, QR login, sync, messaging, or campaigns
Keep existing workspace isolation
Keep current dark visual identity
Prefer incremental component refactors over a full rewrite
```

---

## 10. Implementation Order

```text
1. Build shared toast/notification component.
2. Redesign Account tab onboarding states.
3. Improve Zalo and contact-sync status cards.
4. Replace campaign recipient search with Choose From Contacts workflow.
5. Simplify campaign form hierarchy.
6. Add compact campaign progress summary and scrollable event log.
7. Improve recent campaigns list.
8. Add shared loading, validation, and error states.
9. Test desktop and mobile responsiveness.
10. Run regression tests on all existing flows.
```

---

## Acceptance Criteria

```text
Account tab clearly guides a new user from signup to messaging
QR and contact-sync states are easy to understand
Campaign recipients are selected through one clear contact-picker workflow
Standalone Search Contacts field is removed
Campaign progress no longer expands the entire page
Detailed logs are scrollable/collapsible
Success and failure events trigger clear in-app notifications
Buttons prevent duplicate requests
Existing backend flows continue to work
Desktop and mobile layouts remain usable
```

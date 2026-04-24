# Zalo Automation Webtool: Architecture & Implementation Plan

## 1. Executive Summary
This document outlines the architecture and implementation strategy for a centralized Web Dashboard designed to automate Zalo interactions. By utilizing browser automation (Playwright) via session cookies, the system bypasses the lack of an open API for personal Zalo accounts.

The core objective is to build a scalable system capable of linking accounts, syncing contacts, and executing targeted mass-sending campaigns directly from the system's database.

---

## 2. System Architecture
The system relies on an asynchronous, decoupled architecture to ensure the web interface remains fast while heavy browser automation runs in the background.

* **Frontend (Dashboard):** A web interface (React/Vue) where users input target criteria, draft messages, and manage their Zalo Cookies.
* **Backend (API Server):** Built with FastAPI (Python) to handle HTTP requests, complex database queries, and task delegation.
* **Database:** PostgreSQL for robust relational data storage (User Credentials, Cookies, Contacts Table, Campaign Logs).
* **Message Broker & Task Queue:** Redis + Celery. Essential for queueing Playwright tasks so they execute sequentially.
* **Automation Worker (Zalo Driver):** Python scripts utilizing `Playwright` in headless mode. The worker acts as the execution engine for DOM manipulation and messaging.

---

## 3. Implementation Phases

### Phase 1: Authentication & Session Management
The system uses browser Cookies to authorize sessions without requiring repeated manual QR logins.
* **Extraction & Storage:** The user extracts their `zalo.me` cookies via their browser console and inputs them into the dashboard. FastAPI stores these securely in the database.
* **Session Injection:** When a worker spins up, Playwright initializes a new browser context and injects the stored cookies, instantly authenticating the session.

### Phase 2: Database-Driven Mass Sending Campaigns
This phase implements the core campaign workflow, allowing users to target specific segments of their synchronized contact list. 

The workflow is strictly defined by 4 steps:
1. **Sync:** (Driven by Phase 4) New Zalo friends and incoming chats are automatically pushed into the system's `Contacts` database table.
2. **Filter:** The user opens the Web Dashboard, creates a new campaign, and filters the database via the UI (e.g., *"Target all contacts tagged as 'new_lead' added in the last 7 days"*).
3. **Query:** FastAPI receives the filter parameters, queries the PostgreSQL database, and retrieves the exact list of matching phone numbers or contact IDs.
4. **Execute:** FastAPI pushes this compiled list to the Celery worker queue. The Playwright worker sequentially navigates to each contact in the headless browser, types the drafted message, and sends it, utilizing randomized delays (15–30 seconds) to bypass anti-bot detection.

### Phase 3: Intelligent Auto-Reply System
The system listens for incoming messages and responds autonomously.
* **DOM Scanning:** The worker monitors the web interface for unread message badges.
* **NLP Processing:** To process customer intents beyond basic keyword matching, a custom NLP pipeline can be integrated. Deploying a lightweight Transformer model via PyTorch allows the system to handle Vietnamese tokenization effectively. By utilizing custom attention mechanisms, the model can accurately extract context from incoming messages and route them to the correct predefined response templates.

### Phase 4: Contact Synchronization
To power the "Sync" step in Phase 2, the system must continuously update the database with new Zalo connections.
* **Optimized Polling:** The worker routinely checks the navigation sidebar for a notification badge on the "Contacts" icon. It only navigates to the contact tab and updates the database if a badge is detected.
* **MutationObserver:** Custom JavaScript is injected into the Playwright context to monitor the DOM tree. When a new contact node appears in the UI, a webhook is fired back to the FastAPI server to update the `Contacts` table in real-time.

---

## 4. Deployment Strategy
* **Environment:** Hosting the backend services, message brokers, and Playwright workers on a Linux/Ubuntu environment ensures optimal stability and compatibility for headless browser execution.
* **Containerization:** Use Docker and Docker Compose to bundle FastAPI, Redis, Celery, and the Playwright binaries. This ensures identical environments across development and production.
* **Monitoring:** Implement extensive logging for the Playwright workers to track DOM selector failures, as Zalo frequently updates their HTML structure.
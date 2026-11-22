# Deploy AlertsOnStandard on OpenShift (Web Console)

This guide explains how to deploy the **AlertsOnStandard** stack using only the OpenShift web console. It assumes you do not use the `oc` CLI.

The stack consists of three services:

1. **compliance** — Python 3.11+ + FastAPI service that evaluates Grafana alert rules.
2. **ui-backend** — Python 3.12 + FastAPI API backend. It replaced a Node.js/Express service with the same API; see [10. ui-backend: the Python service](#10-ui-backend-the-python-service).
3. **ui-frontend** — React 18 + Vite SPA served by nginx.

> This project is designed for an **on-prem/air-gapped** environment. All images must be built from a registry that OpenShift can reach.

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [High-level architecture](#high-level-architecture)
- [1. Create the project](#1-create-the-project)
- [2. Build or import the container images](#2-build-or-import-the-container-images)
- [3. Create ConfigMap and Secret](#3-create-configmap-and-secret)
- [4. Create Deployments and Services](#4-create-deployments-and-services)
- [5. Create the Route](#5-create-the-route)
- [6. Verify the deployment](#6-verify-the-deployment)
- [7. Security notes](#7-security-notes)
- [8. Grafana self-signed certificates](#8-grafana-self-signed-certificates)
- [9. Updating the application](#9-updating-the-application)
- [10. ui-backend: the Python service](#10-ui-backend-the-python-service)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

- Access to the OpenShift web console with permissions to create:
  - Projects
  - Deployments
  - Services
  - Routes
  - ConfigMaps
  - Secrets
  - Builds (optional, if building inside OpenShift)
- A container registry accessible to OpenShift:
  - The OpenShift internal registry, **or**
  - A private on-prem registry (Harbor, Nexus, Artifactory, etc.).
- A Grafana instance reachable from the cluster.
- Network access from your workstation to push images if you build locally.

---

## High-level architecture

```
┌──────────────────────────────────────────────────┐
│                    OpenShift                     │
│                                                  │
│  ┌─────────────┐   Route (HTTPS)   ┌───────────┐ │
│  │ ui-frontend │ <──────────────── │   Users   │ │
│  │   (nginx)   │                   │ (browser) │ │
│  └──────┬──────┘                   └───────────┘ │
│         │ /api proxy                             │
│         ▼                                        │
│  ┌─────────────┐  (Python/FastAPI)               │
│  │ ui-backend  │                                 │
│  │   :3000     │                                 │
│  └──────┬──────┘                                 │
│         │ internal Service                       │
│         ▼                                        │
│  ┌─────────────┐  (Python/FastAPI)               │
│  │ compliance  │                                 │
│  │   :8000     │                                 │
│  └──────┬──────┘                                 │
│         │ outbound to Grafana                    │
│         ▼                                        │
│  ┌─────────────┐                                 │
│  │   Grafana   │                                 │
│  └─────────────┘                                 │
└──────────────────────────────────────────────────┘
```

The UI backend and compliance service are **not** exposed externally. The browser talks to the frontend Route only, and nginx proxies `/api` calls to the UI backend Service.

---

## 1. Select the existing vena project

1. Log in to the OpenShift web console.
2. In the top navigation bar, click the project dropdown.
3. Select the existing project named **vena**.

All resources in this guide should be created inside the **vena** project.

---

## 2. Build or import the container images

You have two options. Choose the one that fits your environment.

### Option A: Build locally and push to a registry

1. On your workstation, build the three images with Podman or Docker:

   ```bash
   # Example commands; run them in a terminal, not in OpenShift
   # (this is the path the cluster actually pulls from, per the live Deployment)
   export REGISTRY=8200artifactory.d8200.mil/docker-images-local/vena/alerts-migration/alerts-migration
   export TAG=1.0.3

   podman build \
     --build-arg POETRY_HTTP_BASIC_PYPI_USERNAME="$POETRY_HTTP_BASIC_PYPI_USERNAME" \
     --build-arg POETRY_HTTP_BASIC_PYPI_PASSWORD="$POETRY_HTTP_BASIC_PYPI_PASSWORD" \
     -t $REGISTRY/compliance:$TAG ./migration-compliance

   # PIP_INDEX_URL is optional: a PyPI mirror when pypi.org is not reachable
   podman build \
     --build-arg PIP_INDEX_URL="$PIP_INDEX_URL" \
     -t $REGISTRY/ui-backend:$TAG ./migration-ui-backend
   podman build -t $REGISTRY/ui-frontend:$TAG ./migration-frontend

   podman push $REGISTRY/compliance:$TAG
   podman push $REGISTRY/ui-backend:$TAG
   podman push $REGISTRY/ui-frontend:$TAG
   ```

   > The `ui-frontend` Dockerfile already uses the unprivileged nginx image and listens on port **8080** so it can run under OpenShift's default security policy.

2. If you use a private external registry, create an image pull secret in the OpenShift UI:

   - Go to **Workloads > Secrets**.
   - Click **Create > Image pull secret**.
   - Enter your registry server, username, and password.
   - Name it `regcred`.
   - Go to **Workloads > ServiceAccounts**, open `default`, and add `regcred` under **Image pull secrets**.

### Option B: Build inside OpenShift from Git

1. Make sure OpenShift can reach your GitLab repository.
2. In the Developer perspective, click **+Add**.
3. Choose **Import from Git**.
4. Enter the repository URL and select the branch (for example, `epic-4-bulk-transform`).
5. In the builder options, choose **Dockerfile** as the build strategy.
6. Set the **Context dir** for each build:
   - `migration-compliance`
   - `migration-ui-backend`
   - `migration-frontend`
7. Repeat the import three times to build all images.
8. OpenShift will store the images as Image Streams in the `vena` project.

---

## 3. Create ConfigMap and Secret

### Create the ConfigMap

1. In the OpenShift console, go to **Workloads > ConfigMaps**.
2. Click **Create ConfigMap**.
3. Switch to the **YAML view** and paste the following YAML:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: vena-config
  namespace: vena
data:
  UI_BACKEND_PORT: "3000"
  COMPLIANCE_SERVICE_URL: "http://compliance:8000"
  GRAFANA_URL: "https://your-grafana-instance.d8200.mil"
  CORS_ORIGIN: "https://vena-vena.apps.ocp4-prep.d8200.mil"
  SESSION_TTL_MINUTES: "10080"
  SESSION_MAX_TTL_DAYS: "30"
  SESSION_COOKIE_SECURE: "auto"
  LOG_LEVEL: "info"
  REGISTRY_DATA_DIR: "/data"
  COMPLIANCE_PORT: "8000"
  COMPLIANCE_LOG_LEVEL: "info"
  COMPLIANCE_CORS_ALLOWED_ORIGINS: "https://vena-vena.apps.ocp4-prep.d8200.mil"
  GRAFANA_INSECURE_TLS: "false"
```

4. Replace `https://vena.apps.d8200.mil` with your actual public Route URL. `CORS_ORIGIN` may list several origins, separated by commas.
5. Replace `https://your-grafana-instance.d8200.mil` with your Grafana URL.
6. Click **Create**.

### Create the Secret

1. Go to **Workloads > Secrets**.
2. Click **Create > Key/value secret**.
3. Enter:
   - **Secret name:** `vena-secrets`
   - Add the following keys:
     - **Key:** `SESSION_SECRET` — a random string of at least 16 characters. You can generate one locally with:

       ```bash
       openssl rand -base64 32
       ```

     - **Key:** `GRAFANA_ADMIN_TOKEN` — **required** (or the username/password pair below): a Grafana service-account token (starts with `glsa_`) with the org **Admin** role. After login, every Grafana and compliance-service call of the ui-backend uses this account, after the ui-backend's own folder-permission check. Takes precedence over `GRAFANA_ADMIN_USERNAME` / `GRAFANA_ADMIN_PASSWORD` when set. The ui-backend refuses to start without admin credentials.
     - **Key:** `GRAFANA_ADMIN_USERNAME` — (alternative to the token) a Grafana admin username for Basic auth.
     - **Key:** `GRAFANA_ADMIN_PASSWORD` — (alternative to the token) that admin's password.
     - **Key:** `MIGRATION_STATUS_TOKEN` — a random string (`openssl rand -base64 32`) that alerts-bi sends to read the migrated status of its alerts. Put the **same value** in alerts-bi's Secret. Without it, `POST /api/rules/migration-status` answers 404.
4. Click **Create**.

> Do **not** commit this secret to Git.

---

## 4. Create Deployments and Services

For each of the three services below, do the following in the OpenShift console:

1. Switch to the **Developer** perspective.
2. Click **+Add**.
3. Click **YAML** (or go to **Workloads > Deployments** and click **Create Deployment**).
4. Paste the YAML.
5. Update the `image:` value to point to your actual image location.
6. Click **Create**.

OpenShift may automatically create a Service when you create a Deployment. If not, create the matching Service manually from the same YAML.

### compliance

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: compliance
  namespace: vena
  labels:
    app: compliance
spec:
  replicas: 1
  selector:
    matchLabels:
      app: compliance
  template:
    metadata:
      labels:
        app: compliance
    spec:
      securityContext:
        runAsNonRoot: true
      containers:
        - name: compliance
          image: 8200artifactory.d8200.mil/docker-images-local/vena/alerts-migration/alerts-migration/migration-compliance:1.0.1
          imagePullPolicy: Always
          ports:
            - containerPort: 8000
              name: http
          envFrom:
            - configMapRef:
                name: vena-config
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              cpu: 500m
              memory: 512Mi
          readinessProbe:
            httpGet:
              path: /api/v1/health
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /api/v1/health
              port: 8000
            initialDelaySeconds: 15
            periodSeconds: 20
---
apiVersion: v1
kind: Service
metadata:
  name: compliance
  namespace: vena
spec:
  selector:
    app: compliance
  ports:
    - port: 8000
      targetPort: 8000
```

### ui-backend

The migrations registry (a SQLite file written to `REGISTRY_DATA_DIR`) must survive pod restarts — attach a PVC (PLAN-v1-migrated-flag.md §4.2). Keep **one replica** and the **`Recreate`** strategy: two processes must never use the registry file at the same time (see [10. ui-backend: the Python service](#10-ui-backend-the-python-service)).

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ui-backend-registry
  namespace: vena
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ui-backend
  namespace: vena
  labels:
    app: ui-backend
spec:
  # One replica, one process: only one process may use the SQLite files on the
  # PVC (registry and sessions).
  replicas: 1
  # Recreate stops the old pod before the new one starts, so an image change
  # never runs two processes against the registry file.
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: ui-backend
  template:
    metadata:
      labels:
        app: ui-backend
    spec:
      securityContext:
        runAsNonRoot: true
        # Must match the image's USER 1001 (pinned in the ui-backend
        # Dockerfile). fsGroup makes a freshly provisioned PVC group-writable
        # so the non-root process can create the registry SQLite file at
        # /data.
        runAsUser: 1001
        fsGroup: 1001
      containers:
        - name: ui-backend
          image: 8200artifactory.d8200.mil/docker-images-local/vena/alerts-migration/alerts-migration/migration-ui-backend:1.0.3
          imagePullPolicy: Always
          ports:
            - containerPort: 3000
              name: http
          envFrom:
            - configMapRef:
                name: vena-config
            - secretRef:
                name: vena-secrets
          volumeMounts:
            - name: registry
              mountPath: /data
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              cpu: 500m
              memory: 512Mi
          readinessProbe:
            httpGet:
              path: /api/health
              port: 3000
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /api/health
              port: 3000
            initialDelaySeconds: 15
            periodSeconds: 20
      volumes:
        - name: registry
          persistentVolumeClaim:
            claimName: ui-backend-registry
---
apiVersion: v1
kind: Service
metadata:
  name: ui-backend
  namespace: vena
spec:
  selector:
    app: ui-backend
  ports:
    - port: 3000
      targetPort: 3000
```

### ui-frontend

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ui-frontend
  namespace: vena
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ui-frontend
  namespace: vena
  labels:
    app: ui-frontend
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ui-frontend
  template:
    metadata:
      labels:
        app: ui-frontend
    spec:
      serviceAccountName: ui-frontend
      securityContext:
        runAsNonRoot: true
      containers:
        - name: ui-frontend
          image: 8200artifactory.d8200.mil/docker-images-local/vena/alerts-migration/alerts-migration/migration-frontend:1.0.3
          imagePullPolicy: Always
          ports:
            - containerPort: 8080
              name: http
          envFrom:
            - configMapRef:
                name: vena-config
          resources:
            requests:
              cpu: 50m
              memory: 128Mi
            limits:
              cpu: 200m
              memory: 256Mi
          readinessProbe:
            httpGet:
              path: /
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /
              port: 8080
            initialDelaySeconds: 15
            periodSeconds: 20
---
apiVersion: v1
kind: Service
metadata:
  name: ui-frontend
  namespace: vena
spec:
  selector:
    app: ui-frontend
  ports:
    - port: 8080
      targetPort: 8080
```

> Replace `8200artifactory.d8200.mil/docker-images-local/vena/alerts-migration/alerts-migration` with your actual registry path. CI also pushes with short-SHA tags (e.g. `migration-ui-backend:11f3c8ca`); either tag works as long as the image was pushed. If you use the OpenShift internal registry, the image will look like `image-registry.openshift-image-registry.svc:5000/vena/<service>:<tag>`.

---

## 5. Create the Route

Expose only the frontend. The UI backend is reached through the internal Service and the nginx `/api` proxy.

1. Go to **Networking > Routes**.
2. Click **Create Route**.
3. Fill in:
   - **Name:** `vena`
   - **Hostname:** `vena.apps.d8200.mil` (use your cluster domain)
   - **Service:** `ui-frontend`
   - **Target port:** `8080 -> 8080`
4. Expand **Secure route** and select:
   - **TLS termination:** Edge
   - **Insecure traffic:** Redirect
5. Click **Create**.

OpenShift will generate the URL. You can find it in **Networking > Routes** under the **Location** column.

---

## 6. Verify the deployment

1. Go to **Workloads > Deployments** and confirm all three Deployments show a green checkmark with **1/1** pods.
2. If any pod is failing, open it and click **Logs** to see the error.
3. Go to **Networking > Routes** and click the frontend URL.
4. Log in with your Grafana credentials and test loading a rule or folder.

---

## 7. Security notes

- The Deployments set `runAsNonRoot: true`. OpenShift assigns a random non-root UID automatically — except ui-backend, which is pinned to `runAsUser: 1001` / `fsGroup: 1001` so the non-root process can write the registry PVC (the image also pins `USER 1001`).
- The frontend image uses an internal unprivileged nginx base image (`8200artifactory.d8200.mil/docker-virtual/nginx:1.10.0`) and listens on port **8080**, so it does not require the `anyuid` SCC.
- The UI backend and compliance images run as non-root because their files are world-readable and they do not write to protected paths.
- The ui-backend marks the session cookie `Secure` only when the request reached it over HTTPS, as reported by the first `X-Forwarded-Proto` value. The frontend's nginx sets that header to its own `$scheme`, which is `http` behind an edge-terminated Route, so today the cookie goes out without `Secure`. The fix belongs in `migration-frontend/nginx.conf.template` (pass on the Route's `X-Forwarded-Proto`). The Route must use HTTPS either way.
- Store `SESSION_SECRET` only in the Secret. Rotate it regularly; rotating it logs everyone out.
- The ui-backend never stores a password. At login Grafana checks it (and LDAP behind Grafana); the session keeps only the login, user id, org role and teams, and the session table stores a SHA-256 of the session id, not the id.
- After login, all Grafana and compliance-service calls use the admin account. Before each one the ui-backend checks the user's own folder permissions (View to read, Edit to change), so users see and change only what Grafana would let them. Org admins pass every folder check, as in Grafana.
- Backtests run their query through Grafana with the admin account, so any logged-in user can query whatever datasources that account can read.
- Requests that change something (POST, PUT, DELETE) are refused with 403 when their `Origin` header is neither in `CORS_ORIGIN` nor the ui-backend's own host. `POST /api/rules/migration-status` is exempt; it is protected by `MIGRATION_STATUS_TOKEN`.

---

## 8. Grafana self-signed certificates

If your Grafana instance uses a self-signed certificate, update the ConfigMap values:

1. Go to **Workloads > ConfigMaps**.
2. Open `vena-config`.
3. Click **Actions > Edit ConfigMap**.
4. Change:
   - `GRAFANA_INSECURE_TLS` to `true`
   - `COMPLIANCE_GRAFANA_INSECURE_TLS` to `true`
5. Click **Save**.

OpenShift will roll out the Deployments automatically because the environment variables changed.

> For production, prefer mounting the Grafana CA certificate into the pods instead of disabling TLS verification.

---

## 9. Updating the application

After code changes:

1. Build and push the new image version locally, or trigger a new OpenShift Build from Git.
2. In the OpenShift console, go to **Workloads > Deployments**.
3. Click the Deployment you want to update.
4. Click **Actions > Edit Deployment**.
5. Change the image tag in the container spec, for example from `migration-ui-backend:1.0.2` to `migration-ui-backend:1.0.3`.
6. Click **Save**. OpenShift will roll out the new version.

---

## 10. ui-backend: the Python service

The ui-backend is a Python 3.12 + FastAPI service (`app/` in `migration-ui-backend`). It keeps the `/api` routes and responses of the Express service it replaced and adds `POST /api/rules/load-list` (several rules at once) and `POST /api/rules/migration-status` (for alerts-bi). The Express sources (`src/`) stay in the repository until the cutover is complete.

### Environment variables

A `.env` file in the working directory is read too, as before; real environment variables win.

| Variable | Default | Notes |
|---|---|---|
| `UI_BACKEND_PORT` | `3000` | Port the service listens on. |
| `COMPLIANCE_SERVICE_URL` | `http://localhost:8000` | Must be a URL. |
| `GRAFANA_URL` | `http://localhost:3000` | Must be a URL. |
| `SESSION_SECRET` | `change-me-in-production` | At least 16 characters. Signs the session cookie. |
| `SESSION_TTL_MINUTES` | `10080` (7 days) | Idle timeout. Use restarts it. |
| `SESSION_MAX_TTL_DAYS` | `30` | Absolute cap from login, however active the session is. |
| `SESSION_TOUCH_INTERVAL_SECONDS` | `300` | A session's "last used" time is written at most this often. |
| `SESSION_RECHECK_HOURS` | `24` | How often each session's user is re-checked with the admin account. |
| `SESSION_COOKIE_SECURE` | `auto` | `auto` (Secure when `X-Forwarded-Proto` says https), `true` or `false`. |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warn` or `error`. |
| `CORS_ORIGIN` | `http://localhost:5173` | Origins allowed to call the API with credentials; one, or a comma-separated list. |
| `REGISTRY_DATA_DIR` | `./data` | Holds `migrations.db` and `sessions.db`. On OpenShift: `/data`, the PVC. |
| `GRAFANA_ADMIN_TOKEN` | — | **Required** unless the username/password pair is set. Service-account token; wins over the pair. |
| `GRAFANA_ADMIN_USERNAME`, `GRAFANA_ADMIN_PASSWORD` | — | Basic-auth alternative to the token. |
| `GRAFANA_INSECURE_TLS` | `false` | Exactly `true` turns off TLS verification for Grafana calls. |
| `MIGRATION_STATUS_TOKEN` | — | Bearer token alerts-bi sends to `/api/rules/migration-status`. Unset: the route answers 404. |

The service refuses to start when a value is invalid (for example a `SESSION_SECRET` shorter than 16 characters) or when no admin credentials are set.

### One replica, one process, `Recreate`

- The image runs `python -m app`: uvicorn with **one worker** on `UI_BACKEND_PORT`, as user 1001.
- Sessions are in `sessions.db` on the PVC, so they survive restarts. Some state is per process: the re-check retry times and an hourly cache of team members.
- Keep `replicas: 1` and `strategy: Recreate` (section 4). With `RollingUpdate`, the new pod starts while the old one still runs, and both would use the SQLite files.

### Sessions

- `sessions.db` sits next to `migrations.db` on the same PVC. It is a separate file so that `migrations.db` stays readable by the Express service for a rollback.
- A session lasts while it is used at least once per `SESSION_TTL_MINUTES` (7 days), and never longer than `SESSION_MAX_TTL_DAYS` (30 days) after login. Expired rows are deleted at startup and every hour; logout deletes the row at once.
- **Daily re-check:** the password is only checked at login, so once a day (`SESSION_RECHECK_HOURS`) the ui-backend looks the user up again with the admin account, on their next request. A user Grafana no longer has (or has disabled) is logged out; everyone else gets their org role and teams refreshed.
  - With a Grafana **server admin** account it uses `/api/users/lookup` and `/api/users/{id}/orgs|teams`.
  - Otherwise (403) it uses the org endpoints: `/api/org/users/search` for the user and their role, and `/api/teams/search` plus `/api/teams/{id}/members` for teams, cached for an hour. The org Admin role is enough for these.
  - If Grafana is down or answers with an error, the session is kept and the check is retried about 5 minutes later. Nobody is logged out by an outage.
- **People who leave:** the re-check sees what Grafana knows. A user removed from LDAP is not detected while their Grafana user record still exists; open-source Grafana updates that record only when the user logs in through LDAP again. The 30-day cap bounds how long such a session can last; removing or disabling the user in Grafana ends it at the next re-check.

### Migration status for alerts-bi

`POST /api/rules/migration-status` with `Authorization: Bearer <MIGRATION_STATUS_TOKEN>` and `{"rule_uids": [...]}` (1 to 100) answers `{"migrated": [...]}`: the rules whose latest migration has a v2 copy that still exists. It needs no session and makes at most one Grafana call (a listing of all rules), since alerts-bi waits only about a second.

### The registry file

The Python service opens the same `migrations.db` with the same tables. No data migration is needed.

- While it runs, the file is in SQLite's WAL mode: a `migrations.db-wal` and a `migrations.db-shm` file sit next to it on the PVC.
- On a clean stop (SIGTERM, which Kubernetes sends first), the service folds the WAL back into `migrations.db`, deletes the extra files and leaves the plain format the Express service reads.
- **Never run the Express and the Python service against the file at the same time.** The Express service (sql.js) keeps the whole database in memory and rewrites the file on every write, so it would wipe rows the other process wrote. A `ReadWriteOnce` PVC does not prevent this when both pods land on the same node; `Recreate` does.

### Cutover from Express

1. Build and push the image from `migration-ui-backend` (section 2). The repository's `Dockerfile` now builds the Python service.
2. Make sure the ui-backend Deployment has `strategy: Recreate` (section 4).
3. Add `MIGRATION_STATUS_TOKEN` to `vena-secrets` and check the admin credentials are there: the Python service does not start without them.
4. Change the image tag to the new one and save. OpenShift stops the Express pod, then starts the Python pod.
5. **Everyone is logged out once:** the Express sessions were in memory. From then on sessions survive restarts and deploys. Migrated flags and the seeder's history carry over from the file.

### Rollback to Express

The file stays compatible in both directions, so rolling back means redeploying the Express image:

1. Scale the ui-backend Deployment to **0** and wait until its pod is gone. The clean stop folds the WAL into `migrations.db`.
2. If the last Python pod did **not** stop cleanly (OOMKilled, or killed after `terminationGracePeriodSeconds`), a `migrations.db-wal` file may remain. Do not start Express on it: sql.js ignores that file, so its last writes would be lost, and the next Python start would replay the stale WAL over Express's writes. Instead, set the Python image again, scale to 1, wait until the pod is Ready, then scale back to 0. That clean stop removes the WAL.
3. Change the image back to the Express tag (for example `migration-ui-backend:1.0.3`) and scale to 1.
4. Users log in again. Rows the Python service recorded are kept. Express ignores `sessions.db`; it can stay on the PVC.

### Running it locally

```bash
cd migration-ui-backend
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# The CI checks
.venv/bin/ruff check app tests
.venv/bin/mypy app tests
.venv/bin/pytest -q

# Serve on UI_BACKEND_PORT (default 3000); point it at Grafana and the compliance service
GRAFANA_URL=https://grafana.example COMPLIANCE_SERVICE_URL=http://localhost:8000 \
  .venv/bin/python -m app
```

---

## Troubleshooting

### Frontend pod fails with `permission denied`

The default `nginx:alpine` image requires root. The repository already uses an internal unprivileged nginx base image (`8200artifactory.d8200.mil/docker-virtual/nginx:1.10.0`) and port 8080. If you still see permission errors, make sure the container image was rebuilt after the Dockerfile change.

### CORS errors in the browser

- Check that the `CORS_ORIGIN` value in the ConfigMap matches the public Route URL exactly, including `https://`.
- Make sure the browser is using the Route, not a local port.
- The frontend calls `/api` on the same origin; nginx proxies those calls to the UI backend Service.

### Cannot reach Grafana

- Check that the cluster can reach Grafana from the ui-backend and compliance pods.
- If Grafana uses a self-signed certificate, set `GRAFANA_INSECURE_TLS` and `COMPLIANCE_GRAFANA_INSECURE_TLS` to `true` in the ConfigMap.

### Compliance service not reachable from ui-backend

- Confirm the Service name is `compliance` and the port is `8000`.
- Confirm both Deployments are in the same project.
- Check the ui-backend pod logs for connection errors.

### Image pull errors

- If using an external registry, verify the image pull secret is attached to the `default` ServiceAccount or to the pod's ServiceAccount (`ui-frontend`).
- Verify the image path and tag are correct.
- If using the OpenShift internal registry, make sure the image was pushed or built successfully and appears under **Builds > ImageStreams**.

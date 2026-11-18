# Deploy AlertsOnStandard on OpenShift (Web Console)

This guide explains how to deploy the **AlertsOnStandard** stack using only the OpenShift web console. It assumes you do not use the `oc` CLI.

The stack consists of three services:

1. **compliance** — Python 3.11+ + FastAPI service that evaluates Grafana alert rules.
2. **ui-backend** — Node.js 20 + Express API backend.
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
│  ┌─────────────┐  (Node/Express)                 │
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

   podman build -t $REGISTRY/ui-backend:$TAG ./migration-ui-backend
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
  SESSION_TTL_MINUTES: "30"
  LOG_LEVEL: "info"
  REGISTRY_DATA_DIR: "/data"
  COMPLIANCE_PORT: "8000"
  COMPLIANCE_LOG_LEVEL: "info"
  COMPLIANCE_CORS_ALLOWED_ORIGINS: "https://vena-vena.apps.ocp4-prep.d8200.mil"
  GRAFANA_INSECURE_TLS: "false"
```

4. Replace `https://vena.apps.d8200.mil` with your actual public Route URL.
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

     - **Key:** `GRAFANA_ADMIN_TOKEN` — a Grafana service-account token (starts with `glsa_`) with admin permissions. This token is used for provisioning API calls (creating/updating alert rules). Takes precedence over `GRAFANA_ADMIN_USERNAME` / `GRAFANA_ADMIN_PASSWORD` when set.
     - **Key:** `GRAFANA_ADMIN_USERNAME` — (alternative to the token) a Grafana admin/service-account username for Basic auth.
     - **Key:** `GRAFANA_ADMIN_PASSWORD` — (alternative to the token) the password for the Grafana admin/service-account.
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
[end of code block]
[heading] ui-backend
[paragraph] The migrations registry (a SQLite file written to `REGISTRY_DATA_DIR`) must survive pod restarts — attach a PVC (PLAN-v1-migrated-flag.md §4.2).
[start of code block]
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
  replicas: 1
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
[end of code block]
[heading] ui-frontend
[start of code block]
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
- Session cookies are marked `secure` in production, so the Route must use HTTPS.
- Store `SESSION_SECRET` only in the Secret. Rotate it regularly.

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

# homelab-gitops-arr

**GitOps source of truth for the `arr` VM's k3s cluster** (VLAN 30 · `192.168.30.254` · the
media-acquisition tier). A single-node ArgoCD (Core mode) on the arr VM watches this repo and
reconciles the cluster to match it — deployment = commit here.

This is the **sibling** of [`homelab-gitops`](https://github.com/Alpostros/homelab-gitops) (the
`cerberus` cluster). The two are kept in **separate repos on purpose** — see *Why two repos*. The
design, phases and decisions live in **homelab-docs**:
[`docs/arr-gitops-plan.md`](https://github.com/Alpostros/homelab-docs/blob/main/docs/arr-gitops-plan.md).

> **Private.** Cluster manifests + Helm values. **No plaintext secrets** — secrets are committed
> only as **SealedSecrets** (encrypted), decrypted in-cluster by the sealed-secrets controller.
> arr's sealed-secrets keypair is its **own**, distinct from cerberus's.

## Two independent ArgoCDs — how they stay separate

One ArgoCD per cluster, each watching only its own repo, each deploying only into itself:

- **No cluster Secret** for any other cluster exists in this `argocd` namespace — so arr's ArgoCD
  has no credential or endpoint for cerberus, and cannot reach it. (Same in reverse.) This is the
  real boundary.
- **`AppProject arr`** ([`bootstrap/project.yaml`](bootstrap/project.yaml)) pins `destinations` to
  `https://kubernetes.default.svc` (controller-enforced — an app naming another server is rejected)
  and `sourceRepos` to this repo.
- **Separate read-only deploy keys** — arr's key opens only this repo.

### Why two repos (not one with `clusters/{cerberus,arr}/`)

A GitHub deploy key **cannot be path-scoped**, and `AppProject.sourceRepos` matches on repo URL
only (no path restriction). One shared repo would therefore have given arr's key — on the lab's
least-trusted host — read access to all of cerberus's manifests, values and SealedSecret ciphertext.
Separate repos keep that closed and let either deploy key be revoked independently. It also avoids a
dangerous in-place restructure of the live cerberus repo.

## What runs on arr (target — all ArgoCD-managed once adopted)

Deployed / adopted in ascending blast-radius order:

1. **sealed-secrets** — the secrets foundation (controller in `kube-system`)
2. **node-exporter** (`monitoring`) — DaemonSet, scraped by Prometheus on cerberus as job `k3s-node`
3. **headlamp-rbac** — the read-only `headlamp-ro` ClusterRole/token cerberus's Headlamp reads arr with
4. **ai** — `open-webui` (NodePort 3000) + `openrouter-free-proxy` (ClusterIP); `searxng` at
   **`replicas: 0` on purpose**
5. **bazarr** (`arr`) — subtitles
6. **seerr** (`arr`) — request UI, **public** (internet → Cloudflare → oracle-gateway → tailnet → `:5055`)
7. **arr-stack** (`arr`) — one pod: **gluetun** (Mullvad WireGuard kill-switch) owns the netns;
   qBittorrent · Prowlarr · Radarr · Sonarr · FlareSolverr are sidecars sharing it. **Adopted last.**

**Not managed here:** k3s itself, the virtiofs mounts (Proxmox-side), Traefik/ServiceLB (k3s built-ins).

## Layout

```
bootstrap/project.yaml    # AppProject arr — apply once (Phase 2), before the root app
bootstrap/root-app.yaml   # App-of-Apps root — watches apps/
apps/<app>.yaml           # thin ArgoCD Application per app → points at helm/<app>
helm/<app>/               # umbrella Helm chart per app (upstream chart(s) as deps + values.yaml)
```

## Bootstrap (Phase 2, on the arr VM)

ArgoCD **Core** (no server/UI) — installed from the pinned upstream manifest, **not** the Helm
chart (the chart has no `server.enabled` toggle):

```bash
kubectl --context arr-lan create namespace argocd
kubectl --context arr-lan apply -n argocd --server-side --force-conflicts \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/<PINNED_VERSION>/manifests/core-install.yaml
```

Then connect this **private** repo (read-only deploy key → `repo-*` Secret in `argocd`), and apply
the project + root:

```bash
kubectl --context arr-lan apply -n argocd -f bootstrap/project.yaml
kubectl --context arr-lan apply -n argocd -f bootstrap/root-app.yaml
```

Drive it with `argocd login --core` (spawns a local API server, authenticates via the kubeconfig) —
there is no web UI to expose on VLAN 30.

## Secrets

**Sealed Secrets** — encrypt-and-commit; the in-cluster controller decrypts. `kubeseal` with default
flags. **Back arr's master key up to 1Password immediately** after install — without it a rebuilt
cluster cannot decrypt anything here. Never commit a raw `Secret`.

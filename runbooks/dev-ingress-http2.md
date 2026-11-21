# Dev ingress HTTP/2 mitigation

The cluster-wide Kubernetes ingress-nginx controller on ORISO Dev is managed
separately from the application Helm release. Keep its client-facing HTTP/2
disabled until a maintained replacement is running. The retired controller
bundles an NGINX version affected by CVE-2026-123007 and CVE-2026-123633 when
HTTP/2 is enabled. The ConfigMap change lets HTTPS clients use HTTP/1.1.
See the [NGINX advisories](https://nginx.org/en/security_advisories.html) and
[controller replacement decision](https://github.com/OpenResilienceInitiative/ORISO-Helm/issues/537).

## Reinstall or reconcile the external Dev controller

Confirm that `kubectl` is connected to the ORISO Dev cluster through the
documented Dev operator access. Add the following setting to the external
controller's ConfigMap manifest before applying it.

```yaml
data:
  use-http2: "false"
```

For a running Dev controller, merge the one key into its existing ConfigMap.
The controller reloads its configuration after the update.

```sh
kubectl -n ingress-nginx patch configmap ingress-nginx-controller --type=merge \
  --patch '{"data":{"use-http2":"false"}}'
```

## Verify on Dev

1. Confirm the ConfigMap reads back `false`. Check that all controller pods are
   ready, the reload succeeded and the effective NGINX configuration has no
   HTTP/2 listener on any replica. Review the restart count and reload errors.
2. Connect to `dev.oriso.org` over TLS and offer both protocols. The server
   should select `http/1.1`, and certificate verification should succeed.

   ```sh
   curl --http2 --fail --silent --show-error --output /dev/null \
     --write-out 'HTTP/%{http_version}\n' https://dev.oriso.org/
   ```

3. Open App and Admin and sign in with test accounts. Verify that protected API
   requests succeed for both surfaces and that static assets, Matrix and the
   service-health dashboard still load.
4. Join a test call with two test users and exchange a chat attachment. Verify
   LiveKit signalling and media, then check ingress error rates in SigNoz.

If those checks fail, investigate while keeping HTTP/2 disabled. The permanent
fix is a supported controller that can receive future security updates.

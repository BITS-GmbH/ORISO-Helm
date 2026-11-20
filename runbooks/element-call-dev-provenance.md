# Verify Element Call provenance before a Dev image update

The Element Call Dev publisher attests the OCI image index built by the
`CI - Main` workflow on `dev`. The verifier binds the image digest to that
exact workflow identity, the `dev` ref and the approved build commit.
It prints only the verified repository and digest on success.

Use Python 3 and a patched GitHub CLI (2.102.0 or newer), with access to the
GitHub attestation API and GHCR. Confirm non-interactive access for the
scheduled operator identity. Use the full source commit and immutable image
index digest from the approved Element Call Dev build.

```bash
verified_image=$(python3 scripts/verify_element_call_dev_image.py \
  "$candidate_image" "$approved_source_commit") || exit 1
```

The check must run before the Deployment changes on both the same-day
and nightly paths. They can share one guarded deployment script. Pass
`verified_image` directly to the image update. The current `:dev` tag can change after verification.

To accept the integration, use the approved signed image and verify that the
resulting Deployment uses the exact digest. Exercise both entry points with
the wrong source commit in a non-mutating test and confirm that neither
reaches its deployment action. A missing verifier, unavailable service or
unsuccessful verification must stop the image update.

This policy covers the Dev build workflow. Numbered-release signing is
tracked in [Element Call PR #180](https://github.com/OpenResilienceInitiative/ORISO-ElementCall/pull/180).
[Element Call provenance issue](https://github.com/OpenResilienceInitiative/ORISO-ElementCall/issues/73) · [GitHub CLI verification](https://cli.github.com/manual/gh_attestation_verify)

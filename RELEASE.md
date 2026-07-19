# Release process

1. Replace `Unreleased` in `CHANGELOG.md` with the release date.
2. Confirm `.codex-plugin/plugin.json`, the changelog, the installed package, and lifecycle fixture all use the same new semantic version. Never publish different plugin behavior under a version already used by another bundle; fix forward with a new version so Codex cannot reuse the old cache identity.
3. Run the source-of-truth local release gate:

   ```bash
   python3 scripts/preflight.py full
   ```

   Local results are PARTIAL; required hosted checks are authoritative. Behavior fixes require an exact regression test, and every plugin payload change requires a strictly greater semantic version. Security or state changes additionally require a threat model, malformed and negative tests, and a fresh final-tree review attested against the exact head SHA. Any later head change invalidates that review.

4. From a new Desktop task, verify one direct same-provider child route. Record `route accepted`; record `used and confirmed` only if the client exposes effective child model/provider/effort metadata.
5. If Claude Fable 5 is included in the release, verify the subscription paths from a first-party Claude login: Fable Planner `create_plan`/`revise_plan` with a different Advisor, and Fable Advisor `review_plan` with root planning. Confirm the pinned primary model, exact allowlisted helper set reported by runtime metadata, effort, status, the bounded approval loop, and disable/restore. Also verify the optional Python API Advisor independently with a disposable dedicated config: setup/status make no model call or secret disclosure; `review_plan` makes one request; exact provider-model echo and `end_turn` are required; redirect, malformed response, refusal, rate limit, and network failure never retry or fall back. An unknown helper model or mismatched API model is a release failure, not an implicit allowlist expansion.
6. If Python API Designer is included, test it with a disposable dedicated config and a local fake endpoint before any paid smoke: setup/status must make no model call or disclose a secret; `create_design` must make exactly one request and require exact model echo, `end_turn`, `DESIGN_COMPLETE`, and a non-empty body. Verify redirect, malformed response, refusal, rate limit, timeout, missing sentinel, empty body, config drift, and model mismatch fail before retry or fallback. Verify Fable Advisor and API Designer launchers can be enabled together and disable restores both families.
7. Merge only after every protected check passes.
8. Create a signed annotated tag named `v<manifest-version>` at the reviewed merge commit.
9. Re-run `python3 scripts/preflight.py full` on the tagged tree, then run `python3 scripts/release_check.py --require-tag` and publish a GitHub release from that tag using the matching changelog section.
10. Upgrade from the previous public version in a clean Codex home, reinstall the plugin, and verify the installed version and skill contents changed before starting a new task. Then verify setup, `status --require-effective`, and disable.

Never move a published release tag. If a release is bad, fix forward with a new version and retain the old tag as provenance.

Before downgrading to a release that predates the saved routing schema, run `disable`
with the current release. In particular, disable schema 5 with version 0.9 before
returning to 0.8. Older versions must fail closed on an unknown state schema rather
than guessing how to restore it.

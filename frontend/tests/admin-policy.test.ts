import assert from "node:assert/strict";
import { test } from "node:test";
import { adminBypass, allowedAdmin, readablePath } from "../src/lib/admin/policy";

const LIST = " chase@broadinstitute.org , Cyakabos@Broadinstitute.org ";

test("admins: exact, case-insensitive, verified only", () => {
  assert.equal(allowedAdmin("chase@broadinstitute.org", true, LIST), true);
  assert.equal(allowedAdmin("CYAKABOS@broadinstitute.org ", true, LIST), true);
  assert.equal(allowedAdmin("chase@broadinstitute.org", false, LIST), false);
  assert.equal(allowedAdmin("chase@broadinstitute.org", "true", LIST), false);
  assert.equal(allowedAdmin("evil-chase@broadinstitute.org", true, LIST), false);
  assert.equal(allowedAdmin("chase@broadinstitute.org.evil.com", true, LIST), false);
  assert.equal(allowedAdmin("", true, ","), false);
  assert.equal(allowedAdmin(null, true, LIST), false);
  assert.equal(allowedAdmin("chase@broadinstitute.org", true, undefined), false);
});

test("dev bypass only in next dev, never on Vercel", () => {
  assert.equal(adminBypass({ ADMIN_DEV_BYPASS: "true", NODE_ENV: "development" }), true);
  assert.equal(adminBypass({ ADMIN_DEV_BYPASS: "true", NODE_ENV: "production" }), false);
  assert.equal(adminBypass({ ADMIN_DEV_BYPASS: "true", NODE_ENV: "development", VERCEL: "1" }), false);
  assert.equal(adminBypass({ ADMIN_DEV_BYPASS: "1", NODE_ENV: "development" }), false);
});

test("proxy reaches only the read API", () => {
  for (const ok of ["me", "overview", "claims", "claims/dapper:Claim.local8780920809654f75198e630e3374dddd", "templates", "tables", "tables/dapper_objects/rows", "activity"]) {
    assert.equal(readablePath(ok), true, ok);
  }
  for (const bad of ["auth-events", "../health", "claims/../../v1/claims", "tables/x;drop/rows", "claims/not-an-id", "overview/extra", ""]) {
    assert.equal(readablePath(bad), false, bad);
  }
});

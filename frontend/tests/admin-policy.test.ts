import assert from "node:assert/strict";
import { test } from "node:test";
import { adminBypass, allowedAdmin, jsonContentType, readablePath, sameOrigin, writablePath } from "../src/lib/admin/policy";

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

const CLAIM = "claims/dapper:Claim.local8780920809654f75198e630e3374dddd";
const READS = [
  "me", "overview", "collections", "collections/pigean", "collections/my-coll.v2_x",
  "collections/pigean/queries/top-genes-with-sets/example", "claims", CLAIM,
  "tables", "tables/dapper_objects/rows", "activity",
  "registration/sources", "registration/jobs", "registration/jobs/Ab_9-x",
  "keys",
  "objects", "objects/classes", "objects/dapper:ScientificAccount.1FmKLxZMYHdplxGK0qxpLjO93kaIr6-B", "objects/dapper:File.local05d7d245",
  "deposits", "accounts", "accounts/dapper:ScientificAccount.1FmKLxZMYHdplxGK0qxpLjO93kaIr6-B",
];
const REVOKE = "keys/key_0123456789abcdef/revoke";
const WRITES = ["registration/check", "registration/jobs", "keys", REVOKE];
// Deposit key paths that are neither readable nor writable.
const BAD_KEY_PATHS = [
  "keys/", "keys/key_x/revoke", "keys/KEY_0123456789abcdef/revoke", "keys/key_0123456789ABCDEF/revoke", "Keys",
  "keys/key_0123456789abcdef0/revoke", "keys/key_0123456789abcde/revoke", "keys/key_0123456789abcdeg/revoke",
  "keys/key_0123456789abcdef", `${REVOKE}/`, `${REVOKE}/x`, "keys/revoke", "keys//revoke", "keys/key_0123456789abcdef/delete",
  "keys/../me", "keys/key_0123456789abcdef/../revoke", "keys/key_0123456789abcdef/revoke/../../me",
  "keys\n", `${REVOKE}\n`, `\n${REVOKE}`, ` ${REVOKE}`, "key", "keys/issue", "api_keys",
];

test("proxy reads only the read API", () => {
  for (const ok of READS) assert.equal(readablePath(ok), true, ok);
  const long = "a".repeat(129);
  for (const bad of [
    "auth-events", "../health", "claims/../../v1/claims", "tables/x;drop/rows", "claims/not-an-id", "overview/extra", "",
    "templates", "registration/check",
    "collections/../overview", "collections/..", "collections/.hidden", "collections/a/../b", "collections/a/b",
    "collections/Pigean", "collections/PIGEAN", `collections/${long}`, "collections/", "collections/pigean/",
    "collections/pigean/queries/Top/example", `collections/pigean/queries/${long}/example`,
    "collections/pigean/queries/top", "collections/pigean/queries/top/example/run", "collections/pigean/queries/../example",
    "registration/jobs/", `registration/jobs/${"j".repeat(65)}`, "registration/jobs/a.b", "registration/jobs/a/b", "registration/jobs/../check",
    "registration", "registration/sources/x", "overview\n", "me\n",
    "objects/", "objects/not-an-id", "objects/classes/x", "objects/dapper:Claim.a/b", "objects/dapper:Claim.a?x=1", "objects/../keys",
    "accounts/", "accounts/not-an-id", "accounts/dapper:Claim.a/texts", "deposits/dep_01", "deposits/",
    REVOKE, ...BAD_KEY_PATHS,
  ]) assert.equal(readablePath(bad), false, JSON.stringify(bad));
  assert.equal(readablePath(`collections/${"a".repeat(128)}`), true);
  assert.equal(readablePath(`registration/jobs/${"j".repeat(64)}`), true);
});

test("proxy writes only the registration and deposit key POSTs", () => {
  for (const ok of WRITES) assert.equal(writablePath(ok), true, ok);
  assert.equal(writablePath("keys/key_ffffffffffffffff/revoke"), true);
  for (const bad of [...READS.filter(p => !WRITES.includes(p)), "registration/check/", "registration/check/x", "registration/jobs/abc",
    "registration/../registration/check", "Registration/check", "registration/check\n", "", "auth-events", ...BAD_KEY_PATHS]) {
    assert.equal(writablePath(bad), false, JSON.stringify(bad));
  }
});

test("writes accept JSON only", () => {
  assert.equal(jsonContentType("application/json"), true);
  assert.equal(jsonContentType("application/json; charset=utf-8"), true);
  assert.equal(jsonContentType("Application/JSON"), true);
  for (const bad of [null, undefined, "", "text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "application/jsonx", "text/json"]) {
    assert.equal(jsonContentType(bad), false, String(bad));
  }
});

test("writes must come from the same origin", () => {
  assert.equal(sameOrigin("https://claims-repository.vercel.app", "claims-repository.vercel.app"), true);
  assert.equal(sameOrigin("http://127.0.0.1:3010", "127.0.0.1:3010"), true);
  assert.equal(sameOrigin("http://localhost:3000", "localhost:3000"), true);
  for (const [origin, host] of [
    [null, "localhost:3000"], [undefined, "localhost:3000"], ["", "localhost:3000"], ["null", "localhost:3000"],
    ["http://localhost:3000", null], ["http://localhost:3000", ""],
    ["http://localhost:3001", "localhost:3000"], ["http://localhost", "localhost:3000"],
    ["https://evil.example", "claims-repository.vercel.app"],
    ["https://claims-repository.vercel.app.evil.example", "claims-repository.vercel.app"],
    ["https://evil.example/claims-repository.vercel.app", "claims-repository.vercel.app"],
    ["https://user@claims-repository.vercel.app", "claims-repository.vercel.app"],
    ["file://claims-repository.vercel.app", "claims-repository.vercel.app"],
    ["https://claims-repository.vercel.app", "evil.example"],
  ] as const) assert.equal(sameOrigin(origin, host), false, `${origin} vs ${host}`);
});

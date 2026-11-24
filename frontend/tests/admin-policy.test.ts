import assert from "node:assert/strict";
import { test } from "node:test";
import { adminBypass, allowedAdmin, deviceReadPath, deviceWritePath, jsonContentType, normalizeUserCode, readablePath, safeNext, sameOrigin, writablePath } from "../src/lib/admin/policy";

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
  assert.equal(writablePath("keys/key_ffffffffffffffff/grants"), true);
  assert.equal(writablePath("keys/key_ffffffffffffffff/grants/dismech/withdraw"), true);
  for (const bad of ["keys/key_ffffffffffffffff/grants/", "keys/key_ffffffffffffffff/grants/Dismech/withdraw",
    "keys/key_ffffffffffffffff/grants/dismech", "keys/not-a-key/grants", "keys/key_ffffffffffffffff/grants/../revoke"]) {
    assert.equal(writablePath(bad), false, bad);
  }
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

test("CLI sign-in paths: their own purpose, never the read or write lists", () => {
  assert.equal(deviceReadPath("device/WDJBMJHT"), true);
  assert.equal(deviceWritePath("device/WDJBMJHT/approve"), true);
  assert.equal(deviceWritePath("device/WDJBMJHT/deny"), true);
  for (const bad of ["device/WDJB-MJHT", "device/wdjbmjht", "device/AEIOUBCD", "device/WDJBMJH", "device/WDJBMJHTX",
    "device/WDJBMJHT/approve/", "device/WDJBMJHT/revoke", "device", "device/"]) {
    assert.equal(deviceReadPath(bad) || deviceWritePath(bad), false, bad);
  }
  for (const path of ["device/WDJBMJHT", "device/WDJBMJHT/approve", "device/WDJBMJHT/deny"]) {
    assert.equal(readablePath(path), false, path);
    assert.equal(writablePath(path), false, path);
  }
  assert.equal(deviceReadPath("device/WDJBMJHT/approve"), false);
  assert.equal(deviceWritePath("device/WDJBMJHT"), false);
  // CLI sessions ride on admin_read and admin_write.
  assert.equal(readablePath("tokens"), true);
  assert.equal(writablePath("tokens/tok_0123456789abcdef/revoke"), true);
  for (const bad of ["tokens/tok_x/revoke", "tokens/TOK_0123456789abcdef/revoke", "tokens/tok_0123456789ABCDEF/revoke", "tokens/tok_0123456789abcdef"]) {
    assert.equal(writablePath(bad) || readablePath(bad), false, bad);
  }
});

test("user codes normalize from what people type", () => {
  assert.equal(normalizeUserCode("wdjb-mjht"), "WDJBMJHT");
  assert.equal(normalizeUserCode(" WDJB MJHT "), "WDJBMJHT");
  assert.equal(normalizeUserCode("WDJB-MJHA"), null);
  assert.equal(normalizeUserCode("WDJB-MJH"), null);
  assert.equal(normalizeUserCode(undefined), null);
});

test("sign-in returns only to console paths", () => {
  assert.equal(safeNext("/admin/device?code=WDJBMJHT"), "/admin/device?code=WDJBMJHT");
  assert.equal(safeNext("/admin/keys"), "/admin/keys");
  for (const bad of ["https://evil.example/admin", "//evil.example/admin", "/administrator.evil", "/admin\\@evil", "/admin/device?code=x y",
    "/admin/../api", "/docs", "", undefined, "/admin/device?next=https://evil.example"]) {
    assert.equal(safeNext(bad), "/admin", String(bad));
  }
});

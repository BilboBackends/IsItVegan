import assert from "node:assert/strict";
import { gzipSync } from "node:zlib";
import test from "node:test";

import { readGzipJson } from "../src/gzipJson.js";


test("decodes a pre-compressed JSON response", async () => {
  const expected = { count: 2, dishes: [{ id: 1 }, { id: 2 }] };
  const compressed = gzipSync(Buffer.from(JSON.stringify(expected)));

  const actual = await readGzipJson(new Response(compressed));

  assert.deepEqual(actual, expected);
});


test("reads a body already decompressed by the host and Fetch", async () => {
  const expected = { count: 1, dishes: [{ id: 7 }] };
  const response = new Response(JSON.stringify(expected), {
    headers: { "Content-Encoding": "gzip" },
  });

  const actual = await readGzipJson(response);

  assert.deepEqual(actual, expected);
});

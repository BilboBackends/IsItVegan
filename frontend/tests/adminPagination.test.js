import assert from "node:assert/strict";
import test from "node:test";

import { limitGroupedRows } from "../src/adminPagination.js";

test("limits rendered rows across groups without losing group membership", () => {
  const first = [{ id: 1 }, { id: 2 }];
  const second = [{ id: 3 }, { id: 4 }];
  const result = limitGroupedRows(
    [
      { label: "A", items: first },
      { label: "B", items: second },
    ],
    3
  );

  assert.deepEqual(result.map((group) => group.items.map((item) => item.id)), [
    [1, 2],
    [3],
  ]);
  assert.equal(result[1].totalCount, 2);
  assert.strictEqual(result[1].allItems, second);
});

test("zero limit renders no groups", () => {
  assert.deepEqual(limitGroupedRows([{ label: "A", items: [{ id: 1 }] }], 0), []);
});

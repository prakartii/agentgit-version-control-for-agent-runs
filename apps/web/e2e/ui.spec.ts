import { expect, test } from "@playwright/test";

const A = "a".repeat(64);
const B = "b".repeat(64);

test("the runs list shows both runs and links a fork to its diff", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Runs" })).toBeVisible();
  await expect(page.getByRole("link", { name: A.slice(0, 12) })).toBeVisible();
  await expect(page.getByRole("link", { name: B.slice(0, 12) })).toBeVisible();
  await expect(page.getByRole("link", { name: "diff vs parent" })).toHaveAttribute(
    "href",
    `/diff?a=${A}&b=${B}`,
  );
});

test("a run page shows latency by tool and expands a step's payloads", async ({ page }) => {
  await page.goto(`/runs/${A}`);
  await expect(page.getByRole("heading", { name: "Latency by tool" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "write_file" })).toBeVisible();

  const step = page.locator("details", { hasText: "write_file" });
  await step.locator("summary").click();
  await expect(step.getByText("result of step 1")).toBeVisible();
  await expect(step.getByText("notes.txt")).toBeVisible();
});

test("a fork page says where it was forked from", async ({ page }) => {
  await page.goto(`/runs/${B}`);
  await expect(page.getByText("Forked from")).toBeVisible();
  await expect(page.getByText("at step 1")).toBeVisible();
});

test("the diff page shows the changed path and both values", async ({ page }) => {
  await page.goto(`/diff?a=${A}&b=${B}`);
  await expect(page.getByText("first difference at step 1")).toBeVisible();
  await expect(page.getByText("result.content[0].text")).toBeVisible();
  await expect(page.getByText("- saved")).toBeVisible();
  await expect(page.getByText("+ EDITED")).toBeVisible();
});

test("an unknown run shows the API's message instead of crashing", async ({ page }) => {
  await page.goto("/runs/does-not-exist");
  await expect(page.getByText("no run matches 'does-not-exist'")).toBeVisible();
});

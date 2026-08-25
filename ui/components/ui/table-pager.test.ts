import { describe, expect, it } from "vitest";

import { paginate, pageWindow, PAGE_SIZE } from "./table-pager";

/**
 * 分页是纯前端切片，所以它的两个边界必须被钉死：
 * **页码越界要夹回合法区间**（筛选一变总数就缩水，停在第 7 页却只剩 2 页
 * 是正常操作序列，不是异常），**页码窗撞到两端要整窗贴边**（否则末页附近
 * 窗口会缩成一两个格子）。这两条都是看着像小事、错了却会让人以为列表空了。
 */
describe("paginate", () => {
  const items = Array.from({ length: 45 }, (_, i) => i);

  it("按页切片，页大小是 PAGE_SIZE", () => {
    const r = paginate(items, 1);
    expect(r.rows).toHaveLength(PAGE_SIZE);
    expect(r.rows[0]).toBe(0);
    expect(r.total).toBe(45);
    expect(r.pages).toBe(3);
  });

  it("末页只给剩下的那些行", () => {
    const r = paginate(items, 3);
    expect(r.rows).toEqual([40, 41, 42, 43, 44]);
    expect(r.page).toBe(3);
  });

  it("页码越界夹回合法区间，不返回空表", () => {
    expect(paginate(items, 99).page).toBe(3);
    expect(paginate(items, 99).rows).toHaveLength(5);
    expect(paginate(items, 0).page).toBe(1);
    expect(paginate(items, -5).page).toBe(1);
    expect(paginate(items, Number.NaN).page).toBe(1);
  });

  it("空列表仍然是第 1 / 1 页", () => {
    const r = paginate([], 1);
    expect(r).toEqual({ rows: [], page: 1, pages: 1, total: 0 });
  });
});

describe("pageWindow", () => {
  it("当前页居中", () => {
    expect(pageWindow(5, 10)).toEqual([3, 4, 5, 6, 7]);
  });

  it("撞到左端时整窗贴边，不缩水", () => {
    expect(pageWindow(1, 10)).toEqual([1, 2, 3, 4, 5]);
    expect(pageWindow(2, 10)).toEqual([1, 2, 3, 4, 5]);
  });

  it("撞到右端时整窗贴边，不缩水", () => {
    expect(pageWindow(10, 10)).toEqual([6, 7, 8, 9, 10]);
    expect(pageWindow(9, 10)).toEqual([6, 7, 8, 9, 10]);
  });

  it("总页数少于窗宽时只出这么多格", () => {
    expect(pageWindow(1, 3)).toEqual([1, 2, 3]);
    expect(pageWindow(1, 1)).toEqual([1]);
  });
});

/**
 * 从原仓库（只读）导出内置食材大全为 JSON 种子数据。
 * 用法：bun run tools/export-foods.ts
 * 原仓库仅被 import 读取，不做任何修改。
 */
import { writeFileSync } from "node:fs";
import { FOODS, FOOD_CATEGORIES, unitFor } from "C:/models/Smart-Recipe-Manager/src/db/foods-data";

const foods = FOODS.map((f) => ({
  name: f.name,
  category: f.category,
  emoji: f.emoji,
  unit: unitFor(f.name, f.category),
}));

const data = { categories: FOOD_CATEGORIES, foods };
const target = new URL("../skills/umami-health/scripts/seed_foods.json", import.meta.url);
writeFileSync(target, JSON.stringify(data, null, 2) + "\n", "utf8");
console.log(`exported ${foods.length} foods, ${FOOD_CATEGORIES.length} categories -> ${target.pathname}`);

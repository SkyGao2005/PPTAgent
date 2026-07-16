// Deck content model + builder, translated from the design demo's
// `PPTAgent frontend design/pptagent-data.js`. Only the mock server uses it;
// pages and stores never import deck internals.

export type DeckLayout =
  | "cover"
  | "toc"
  | "section"
  | "bullets"
  | "split"
  | "chart"
  | "compare"
  | "timeline"
  | "quote"
  | "end"

export interface DeckSlide {
  layout: DeckLayout
  title: string
  subtitle?: string
  items?: string[]
  bullets?: string[]
  img?: string
  bars?: { label: string; v: number }[]
  kpis?: { k: string; v: string }[]
  left?: { t: string; items: string[] }
  right?: { t: string; items: string[] }
  steps?: { q: string; t: string }[]
  quote?: string
  by?: string
  num?: string
  accentIdx: number
}

const POOL: Omit<DeckSlide, "accentIdx">[] = [
  {
    layout: "bullets",
    title: "背景与目标",
    bullets: [
      "外部环境快速变化，既有增长模式面临挑战",
      "本期核心目标：明确方向、对齐节奏、聚焦资源",
      "以数据为依据，以结果为导向，建立季度复盘机制",
      "形成跨部门共识，确保执行落地",
    ],
  },
  {
    layout: "split",
    title: "市场洞察",
    img: "市场趋势图",
    bullets: [
      "行业规模持续扩容，年复合增长率约 12.4%",
      "客户需求向一体化、智能化方案迁移",
      "头部集中度提升，差异化能力成为关键壁垒",
    ],
  },
  {
    layout: "chart",
    title: "核心数据回顾",
    bars: [
      { label: "Q1", v: 52 },
      { label: "Q2", v: 64 },
      { label: "Q3", v: 58 },
      { label: "Q4", v: 81 },
    ],
    kpis: [
      { k: "营收同比", v: "+18.6%" },
      { k: "毛利率", v: "42.3%" },
      { k: "客户留存", v: "91%" },
    ],
  },
  {
    layout: "compare",
    title: "战略框架：双轮驱动",
    left: {
      t: "效率提升",
      items: ["流程数字化改造", "组织结构扁平化", "成本精细化管理"],
    },
    right: {
      t: "增长突破",
      items: ["新市场区域拓展", "高价值客户深耕", "产品矩阵延伸"],
    },
  },
  {
    layout: "bullets",
    title: "重点举措拆解",
    bullets: [
      "举措一：核心产品体验升级，季度内完成三轮迭代",
      "举措二：渠道结构优化，直销占比提升至 45%",
      "举措三：建立客户成功团队，缩短价值兑现周期",
      "举措四：数据中台建设，统一指标口径",
    ],
  },
  {
    layout: "timeline",
    title: "里程碑规划",
    steps: [
      { q: "Q1", t: "完成方案设计与资源盘点" },
      { q: "Q2", t: "试点落地，验证关键假设" },
      { q: "Q3", t: "全面推广，建立运营节奏" },
      { q: "Q4", t: "复盘迭代，沉淀方法论" },
    ],
  },
  {
    layout: "split",
    title: "资源与预算安排",
    img: "预算分布图",
    bullets: [
      "总预算的 60% 投向核心增长项目",
      "预留 15% 弹性额度应对不确定性",
      "按季度滚动审视，动态调配",
    ],
  },
  {
    layout: "bullets",
    title: "风险识别与对策",
    bullets: [
      "市场风险：需求波动 → 建立弹性供给与快速响应机制",
      "执行风险：跨部门协同 → 设立联合作战室与周会机制",
      "人才风险：关键岗位缺口 → 提前储备与轮岗培养",
    ],
  },
  {
    layout: "quote",
    title: "团队共识",
    quote: "把每一次汇报，都当作一次共识的构建。",
    by: "项目组",
  },
  {
    layout: "bullets",
    title: "组织与分工",
    bullets: [
      "指导委员会：方向把关与资源保障",
      "项目组：整体推进与节奏管理",
      "各业务线：目标承接与执行反馈",
    ],
  },
]

const SECTIONS = ["背景与现状", "战略与举措", "规划与展望"]

export function buildDeck(topic: string, pages: number): DeckSlide[] {
  const n = Math.max(5, Math.min(30, pages || 10))
  const contentCount = n - 3
  const secAt = [0]
  if (contentCount >= 5) {
    secAt.push(Math.ceil(contentCount / 2))
  }
  const body: DeckSlide[] = []
  let poolIdx = 0
  let secIdx = 0
  for (let i = 0; i < contentCount; i++) {
    if (secAt.includes(i) && secIdx < SECTIONS.length) {
      body.push({
        layout: "section",
        title: SECTIONS[secIdx],
        num: `0${secIdx + 1}`,
        accentIdx: 0,
      })
      secIdx++
    } else {
      const source = POOL[poolIdx % POOL.length]
      const copy = structuredClone(source) as DeckSlide
      copy.accentIdx = 0
      if (poolIdx >= POOL.length) {
        copy.title = `${copy.title} · 补充`
      }
      body.push(copy)
      poolIdx++
    }
  }
  const slides: DeckSlide[] = [
    {
      layout: "cover",
      title: topic || "未命名演示",
      subtitle: "PPTAgent 生成 · 2026 年 7 月",
      accentIdx: 0,
    },
    {
      layout: "toc",
      title: "目录",
      items: body
        .filter((slide) => slide.layout !== "section")
        .map((slide) => slide.title)
        .slice(0, 6),
      accentIdx: 0,
    },
    ...body,
    { layout: "end", title: "谢谢观看", subtitle: "欢迎交流指正", accentIdx: 0 },
  ]
  return slides.slice(0, n)
}

export function revisedBullets(bullets: string[] | undefined): string[] | undefined {
  if (!bullets) {
    return bullets
  }
  return bullets
    .slice()
    .reverse()
    .map((item) =>
      item.length > 18
        ? item.slice(0, Math.ceil(item.length * 0.75)).replace(/[，,：:]$/, "")
        : item,
    )
}

export function shortBullets(bullets: string[] | undefined): string[] | undefined {
  if (!bullets) {
    return bullets
  }
  return bullets.map((item) => item.split(/[，,→]/)[0])
}

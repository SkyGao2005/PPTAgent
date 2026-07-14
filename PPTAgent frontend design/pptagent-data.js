// PPTAgent shared demo data & deck builder (ES module)

export const TEMPLATES = [
  {
    id: 'obsidian', name: '曜石商务', desc: '深空蓝与鎏金点缀，适合年度汇报与高层提案',
    palette: { bg: '#101522', surface: '#1A2233', primary: '#E8B45A', accent: '#8FA3CC', ink: '#F2EEE6', dark: true },
    slides: 32, ratio: '16:9', status: 'ready',
    layouts: ['封面', '目录', '章节页', '标题+要点', '双栏对比', '左文右图', '数据看板', '时间线', '引言页', '全幅图片', '团队页', '封底']
  },
  {
    id: 'mist', name: '晨雾极简', desc: '低饱和灰蓝，留白充分，适合策略与咨询场景',
    palette: { bg: '#F4F6F8', surface: '#FFFFFF', primary: '#38506B', accent: '#7FA6C9', ink: '#22303E', dark: false },
    slides: 26, ratio: '16:9', status: 'ready',
    layouts: ['封面', '目录', '章节页', '标题+要点', '左文右图', '双栏对比', '数据看板', '引言页', '封底']
  },
  {
    id: 'azure', name: '学术深蓝', desc: '严谨的学术蓝配色，适合研究汇报与课题答辩',
    palette: { bg: '#FFFFFF', surface: '#F2F5FA', primary: '#1D4E89', accent: '#5B8DEF', ink: '#1B2430', dark: false },
    slides: 28, ratio: '4:3', status: 'ready',
    layouts: ['封面', '目录', '章节页', '标题+要点', '左文右图', '数据看板', '时间线', '封底']
  },
  {
    id: 'jade', name: '翡翠年报', desc: '沉稳墨绿，适合 ESG 报告与年度总结',
    palette: { bg: '#F5F7F4', surface: '#FFFFFF', primary: '#1E5C4A', accent: '#8FBCA5', ink: '#20302A', dark: false },
    slides: 30, ratio: '16:9', status: 'ready',
    layouts: ['封面', '目录', '章节页', '标题+要点', '双栏对比', '左文右图', '数据看板', '时间线', '引言页', '封底']
  },
  {
    id: 'amber', name: '琥珀提案', desc: '暖调琥珀，适合品牌提案与创意比稿',
    palette: { bg: '#FBF6EE', surface: '#FFFFFF', primary: '#9C6114', accent: '#E0A63C', ink: '#33291A', dark: false },
    slides: 24, ratio: '16:9', status: 'parsing', progress: 62,
    layouts: ['封面', '目录', '章节页', '标题+要点', '左文右图', '引言页', '封底']
  },
  {
    id: 'graphite', name: '石墨科技', desc: '深色石墨底，适合技术发布与产品评审',
    palette: { bg: '#17181A', surface: '#222428', primary: '#C7C9CE', accent: '#5B8DEF', ink: '#EDEEF0', dark: true },
    slides: 22, ratio: '16:9', status: 'ready',
    layouts: ['封面', '目录', '章节页', '标题+要点', '左文右图', '数据看板', '时间线', '封底']
  },
  {
    id: 'crimson', name: '绯红发布会', desc: '高对比绯红，适合新品发布与市场活动',
    palette: { bg: '#FFF8F7', surface: '#FFFFFF', primary: '#A6252F', accent: '#E4B4B8', ink: '#33191B', dark: false },
    slides: 20, ratio: '16:9', status: 'failed',
    layouts: ['封面', '目录', '章节页', '标题+要点', '全幅图片', '封底']
  },
  {
    id: 'plain', name: '素白纪要', desc: '近乎纯文本的极简版式，适合内部纪要与评审',
    palette: { bg: '#FFFFFF', surface: '#F7F7F8', primary: '#26262A', accent: '#9A9AA2', ink: '#26262A', dark: false },
    slides: 18, ratio: '4:3', status: 'ready',
    layouts: ['封面', '目录', '标题+要点', '左文右图', '封底']
  }
];

export function getTemplate(id) {
  return TEMPLATES.find(t => t.id === id) || TEMPLATES[0];
}

// ---- Deck builder ----

const POOL = [
  { layout: 'bullets', title: '背景与目标', bullets: [
    '外部环境快速变化，既有增长模式面临挑战',
    '本期核心目标：明确方向、对齐节奏、聚焦资源',
    '以数据为依据，以结果为导向，建立季度复盘机制',
    '形成跨部门共识，确保执行落地'
  ]},
  { layout: 'split', title: '市场洞察', img: '市场趋势图', bullets: [
    '行业规模持续扩容，年复合增长率约 12.4%',
    '客户需求向一体化、智能化方案迁移',
    '头部集中度提升，差异化能力成为关键壁垒'
  ]},
  { layout: 'chart', title: '核心数据回顾', bars: [
    { label: 'Q1', v: 52 }, { label: 'Q2', v: 64 }, { label: 'Q3', v: 58 }, { label: 'Q4', v: 81 }
  ], kpis: [
    { k: '营收同比', v: '+18.6%' }, { k: '毛利率', v: '42.3%' }, { k: '客户留存', v: '91%' }
  ]},
  { layout: 'compare', title: '战略框架：双轮驱动',
    left: { t: '效率提升', items: ['流程数字化改造', '组织结构扁平化', '成本精细化管理'] },
    right: { t: '增长突破', items: ['新市场区域拓展', '高价值客户深耕', '产品矩阵延伸'] }
  },
  { layout: 'bullets', title: '重点举措拆解', bullets: [
    '举措一：核心产品体验升级，季度内完成三轮迭代',
    '举措二：渠道结构优化，直销占比提升至 45%',
    '举措三：建立客户成功团队，缩短价值兑现周期',
    '举措四：数据中台建设，统一指标口径'
  ]},
  { layout: 'timeline', title: '里程碑规划', steps: [
    { q: 'Q1', t: '完成方案设计与资源盘点' },
    { q: 'Q2', t: '试点落地，验证关键假设' },
    { q: 'Q3', t: '全面推广，建立运营节奏' },
    { q: 'Q4', t: '复盘迭代，沉淀方法论' }
  ]},
  { layout: 'split', title: '资源与预算安排', img: '预算分布图', bullets: [
    '总预算的 60% 投向核心增长项目',
    '预留 15% 弹性额度应对不确定性',
    '按季度滚动审视，动态调配'
  ]},
  { layout: 'bullets', title: '风险识别与对策', bullets: [
    '市场风险：需求波动 → 建立弹性供给与快速响应机制',
    '执行风险：跨部门协同 → 设立联合作战室与周会机制',
    '人才风险：关键岗位缺口 → 提前储备与轮岗培养'
  ]},
  { layout: 'quote', title: '团队共识', quote: '把每一次汇报,都当作一次共识的构建。', by: '项目组' },
  { layout: 'bullets', title: '组织与分工', bullets: [
    '指导委员会：方向把关与资源保障',
    '项目组：整体推进与节奏管理',
    '各业务线：目标承接与执行反馈'
  ]}
];

const SECTIONS = ['背景与现状', '战略与举措', '规划与展望'];

export function buildDeck(topic, pages) {
  const n = Math.max(5, Math.min(30, pages || 10));
  const slides = [];
  const contentCount = n - 3; // cover + toc + end
  // section break positions among content
  const secAt = [0];
  if (contentCount >= 5) secAt.push(Math.ceil(contentCount / 2));
  const body = [];
  let poolIdx = 0, secIdx = 0;
  for (let i = 0; i < contentCount; i++) {
    if (secAt.includes(i) && secIdx < SECTIONS.length) {
      body.push({ layout: 'section', title: SECTIONS[secIdx], num: '0' + (secIdx + 1) });
      secIdx++;
    } else {
      const p = POOL[poolIdx % POOL.length];
      const copy = JSON.parse(JSON.stringify(p));
      if (poolIdx >= POOL.length) copy.title = copy.title + ' · 补充';
      body.push(copy);
      poolIdx++;
    }
  }
  slides.push({ layout: 'cover', title: topic || '未命名演示', subtitle: '汇报人：李文 · 2026 年 7 月' });
  slides.push({ layout: 'toc', title: '目录', items: body.filter(s => s.layout !== 'section').map(s => s.title).slice(0, 6) });
  slides.push(...body);
  slides.push({ layout: 'end', title: '谢谢观看', subtitle: '欢迎交流指正' });
  return slides.slice(0, n).map((s, i) => Object.assign({ id: 'sl' + i }, s));
}

// alternate phrasing for "内容修订"
export function revisedBullets(bullets) {
  if (!bullets) return bullets;
  const arr = bullets.slice().reverse();
  return arr.map(b => b.length > 18 ? b.slice(0, Math.ceil(b.length * 0.75)).replace(/[，,：:]$/, '') : b);
}

export function shortBullets(bullets) {
  if (!bullets) return bullets;
  return bullets.map(b => b.split(/[，,→]/)[0]);
}

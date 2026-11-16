// 資料まとめファイル（chat-content.json）を作る。設計書 4章。
// クエストアプリの src/data/ を「読むだけ」で、アプリのファイルは書き換えない。
//
// 使い方:
//   node tools/build-content.mjs <クエストアプリのフォルダ> [出力先]
//
// 出力先を省くと build/chat-content.json に書く（build/ は git に入れない）。
// TypeScript のデータをそのまま読むので、Node.js 22.18 以上が必要。

import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

if (!process.argv[2]) {
  console.error('使い方: node tools/build-content.mjs <クエストアプリのフォルダ> [出力先]');
  process.exit(1);
}
const appDir = resolve(process.argv[2]);
const outPath = resolve(process.argv[3] ?? 'build/chat-content.json');

const load = (name) => import(pathToFileURL(resolve(appDir, 'src/data', name)).href);
const { questions } = await load('questions.ts');
const { lectureNotes } = await load('lecture-notes.ts');
const { glossary } = await load('glossary.ts');
const { questionSets } = await load('sets.ts');
const { courseOf, DEFAULT_COURSE, setIdFor, unitLabel } = await load('courses.ts');

// 「10月期 第1回」のような、人が読む呼び名
const labelOf = (course, lecture) => `${courseOf(course)?.label ?? course} ${unitLabel(course, lecture)}`;
const setTitle = (setId) => questionSets.find((s) => s.id === setId)?.title ?? '';
const uniqSorted = (xs) => [...new Set(xs)].sort((a, b) => a - b);

// 要点の文に書かれたページを取り出す。
// 要点にはページの欄がなく、文の終わりに「（p.13〜18, 20）」の形で書いてある。
// 「（第2回 p.10）」のように別の回を指すものは、この回のページではないので取らない。
export function pagesInSentence(sentence) {
  const pages = [];
  for (const [, inside] of sentence.matchAll(/[（(]([^（()）]*)[）)]/g)) {
    if (!inside.includes('p.') || /第\d+回/.test(inside)) continue;
    // 「13〜18」は両端を含めて展開し、「20」はそのまま足す
    for (const [, from, to] of inside.matchAll(/(\d+)(?:\s*[〜~-]\s*(\d+))?/g)) {
      const a = Number(from);
      const b = to ? Number(to) : a;
      for (let p = a; p <= b; p++) pages.push(p);
    }
  }
  return pages;
}

const chunks = [];

// 1. 講義ノート：要点は回ごとに1個、課題は課題ごとに1個
for (const note of lectureNotes) {
  const course = note.course ?? DEFAULT_COURSE;
  const setId = setIdFor(course, note.lecture);
  const label = labelOf(course, note.lecture);
  if (note.summary?.length) {
    chunks.push({
      id: `${setId}-summary`,
      course, set_id: setId, label, kind: 'summary',
      // 問題集がない回（第0回など）は、要点の最初の文から題を作る
      title: `要点：${setTitle(setId) || note.summary[0].split('。')[0].slice(0, 30)}`,
      pages: uniqSorted(note.summary.flatMap(pagesInSentence)),
      text: note.summary.map((s) => `・${s}`).join('\n'),
    });
  }
  note.assignments.forEach((a, i) => {
    chunks.push({
      id: `${setId}-assignment-${i + 1}`,
      course, set_id: setId, label, kind: 'assignment',
      title: `課題：${a.title}`,
      pages: [a.page],
      text: [a.title, a.detail, a.due ? `期限：${a.due}` : ''].filter(Boolean).join('\n'),
    });
  });
}

// 2. 問題の解説：回 × 分野ごとに1個。問題文・正解・解説をまとめる（不正解の選択肢は入れない）
const groups = new Map();
for (const q of questions) {
  if (q.draft) continue;
  const setId = setIdFor(q.course, q.lecture);
  const key = `${setId}\u0000${q.category}`;
  if (!groups.has(key)) groups.set(key, { setId, course: q.course, lecture: q.lecture, category: q.category, items: [] });
  groups.get(key).items.push(q);
}
const countInSet = new Map();
for (const g of groups.values()) {
  const n = (countInSet.get(g.setId) ?? 0) + 1;
  countInSet.set(g.setId, n);
  const terms = [...new Set(g.items.flatMap((q) => q.relatedTerms))].slice(0, 8);
  chunks.push({
    id: `${g.setId}-qa-${n}`,
    course: g.course, set_id: g.setId, label: labelOf(g.course, g.lecture), kind: 'qa',
    title: `解説：${g.category}${terms.length ? `（${terms.join('・')}）` : ''}`,
    pages: uniqSorted(g.items.map((q) => q.source.page).filter((p) => p != null)),
    question_ids: g.items.map((q) => q.id),
    text: g.items
      .map((q) => `問：${q.question}\n答：${q.choices[q.answer]}\n解説：${q.explanation}${q.source.page != null ? `（p.${q.source.page}）` : ''}`)
      .join('\n\n'),
  });
}

// かたまりの id は、AIが選んだ番号を戻す先になる。重なっていたら止める
const ids = chunks.map((c) => c.id);
const dup = ids.filter((id, i) => ids.indexOf(id) !== i);
if (dup.length) {
  console.error(`かたまりの id が重なっています: ${[...new Set(dup)].join(', ')}`);
  process.exit(1);
}

const content = {
  version: new Date().toISOString(),
  chunks,
  glossary: glossary.map(({ term, reading, description }) => ({ term, reading, description })),
};

mkdirSync(dirname(outPath), { recursive: true });
writeFileSync(outPath, JSON.stringify(content, null, 1));

// ---- 数と長さの報告（設計の見込みと比べるため） ----
const len = (s) => [...s].length;
const stat = (xs) => {
  const total = xs.reduce((a, b) => a + b, 0);
  return { n: xs.length, total, avg: xs.length ? Math.round(total / xs.length) : 0, max: Math.max(0, ...xs) };
};
const row = (name, s) => `${name}\t${s.n}\t${s.total}\t${s.avg}\t${s.max}`;
const kindName = { summary: '要点', assignment: '課題', qa: '解説' };

console.log(`出力: ${outPath}`);
console.log(`版: ${content.version}`);
console.log(`元のデータ: 問題 ${questions.length}問 / 講義ノート ${lectureNotes.length}件 / 用語 ${glossary.length}件`);
console.log('');
console.log('種類\t個数\t合計字数\t平均\t最大');
for (const kind of ['summary', 'assignment', 'qa']) {
  console.log(row(kindName[kind], stat(chunks.filter((c) => c.kind === kind).map((c) => len(c.text)))));
}
console.log(row('全部', stat(chunks.map((c) => len(c.text)))));
console.log(row('用語', stat(content.glossary.map((t) => len(t.term + t.description)))));
console.log('');
const noPages = chunks.filter((c) => c.pages.length === 0);
console.log(`ページが1つもないかたまり: ${noPages.length}個${noPages.length ? `（${noPages.map((c) => c.id).join(', ')}）` : ''}`);

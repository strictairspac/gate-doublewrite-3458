import { api } from "../api/workbench.js";
import { errorText, field } from "../components/common/fields.js";
import { empty, pageHeading, section } from "../components/common/panel.js";
import { submit } from "../hooks/submit.js";
import { el } from "../utils/dom.js";
import { cell, download, time } from "../utils/format.js";

function reviewForm(id, saved) {
  const form = el("form", {id: "review-form"},
    field("复核意见", el("textarea", {name: "note", required: true, minlength: 3, maxlength: 500, rows: 3})),
    errorText(), el("button", {type: "submit"}, "签署复核"));
  submit(form, async values => saved(await api.submitReview(id, {note: values.get("note")})));
  return form;
}

function signature(review) {
  return el("div", {"data-testid": "reviewed", class: "signature"},
    el("strong", {}, `已签署 · ${review.actor}`), el("p", {}, review.note), el("time", {}, time(review.created_at)));
}

// 原始业务明细列保持原有顺序与位置；交班列追加在后面。
const BASE_COLUMNS = [
  "页号", "掉电阶段", "持久代", "目标代", "目标校验", "双写代", "双写校验",
  "前缀写入字节", "结论", "说明", "读取文本",
];
const HANDOVER_COLUMNS = ["是否交班", "交班状态", "交班代次", "交班内容"];
const COLUMNS = [...BASE_COLUMNS, ...HANDOVER_COLUMNS];

function handoverStatusCell(row) {
  const handed = Boolean(row.handed_over ?? row["是否交班"]);
  const label = row["交班状态"] ?? row.handover_status_label ?? null;
  const badge = el("span", {
    class: `handover-badge ${handed ? "is-handed" : "is-missing"}`,
    "data-testid": "handover-status",
    "data-handed": handed ? "true" : "false",
  }, handed ? "已交班" : "未交班");
  const nuance = label && label !== (handed ? "已交班" : "未交班")
    ? el("span", {class: "handover-substatus", "data-testid": "handover-detail-status"}, label)
    : null;
  return el("span", {class: "handover-status-cell"}, badge, nuance);
}

function generationCell(row) {
  const generation = row.handover_generation ?? row["交班代次"] ?? row.generation ?? null;
  return el("span", {
    "data-testid": "handover-generation",
    "data-generation": generation === null || generation === undefined ? "" : String(generation),
  }, generation === null || generation === undefined ? "—" : `第${generation}代`);
}

function handoverCell(key, row) {
  const handed = Boolean(row.handed_over ?? row["是否交班"]);
  if (key === "是否交班") return handed; // 交给通用 cell() 渲染成 是/否
  if (key === "交班状态") return handoverStatusCell(row);
  if (key === "交班代次") return generationCell(row);
  // 交班内容
  const text = row["交班内容"] ?? row.handover_content ?? row.handover_text ?? null;
  return text ? el("code", {"data-testid": "handover-content"}, text) : null;
}

function detailTable(rows) {
  return el("div", {class: "table-scroll", tabindex: 0, role: "region", "aria-label": "业务结果明细"},
    el("table", {"data-testid": "result-table"},
      el("thead", {}, el("tr", {}, COLUMNS.map(key => el("th", {scope: "col"}, key)))),
      el("tbody", {}, rows.map(row => {
        const handed = Boolean(row.handed_over ?? row["是否交班"]);
        const page = row["页号"] ?? row.page;
        return el("tr", {
          "data-testid": "handover-row",
          "data-page": page,
          "data-handed": handed ? "true" : "false",
        }, COLUMNS.map(key => {
          let value;
          if (key === "页号") {
            value = el("strong", {"data-testid": "handover-page", "data-page": page}, String(page));
          } else if (HANDOVER_COLUMNS.includes(key)) {
            value = handoverCell(key, row);
          } else {
            value = row[key];
          }
          return el("td", {}, value instanceof Node ? value : cell(value));
        }));
      }))));
}

function handoverOverview(result) {
  const count = Array.isArray(result?.handovers) ? result.handovers.length : 0;
  if (!result?.handover_summary && !count) return null;
  return el("section", {class: "panel handover-overview", "data-testid": "handover-panel"},
    el("h2", {}, "交班记录"),
    el("p", {"data-testid": "handover-summary", class: "handover-hint"},
      result.handover_summary || `本批共 ${count} 页。`,
      " 每页是否已交班、交给下一班的代次见明细表；隔离页沿用上次交班内容，旧代不能覆盖已交班的新代，同代内容不同也会隔离。"));
}

export async function evidencePage(config, user, route) {
  const runs = (await api.runs()).filter(item => item.status === "done");
  const run = route.id ? await api.run(route.id) : runs[0];
  if (!run?.result) return el("div", {class: "page"},
    pageHeading(config.modules[3], null), empty("成功执行一个批次后可以查看、签核和下载证据。"),
    el("a", {href: "#runs"}, config.modules[2]));

  const review = await api.review(run.id);
  const issue = errorText();
  const button = el("button", {type: "button", onclick: async () => {
    button.disabled = true;
    issue.textContent = "";
    try {
      await download(await api.export(run.id), `evidence-${run.id}.json`);
    } catch (error) {
      issue.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  }}, "导出证据");
  const signing = el("div", {});
  signing.append(review ? signature(review) : user.role === "reviewer" ?
    reviewForm(run.id, data => signing.replaceChildren(signature(data))) : empty("待复核员签署。"));
  const options = el("select", {"aria-label": "选择已完成的批次", onchange: event => {
    location.hash = `evidence/${event.target.value}`;
  }}, runs.map(item => el("option", {value: item.id, selected: item.id === run.id}, `#${item.id} ${item.case_title}`)));
  return el("div", {class: "page evidence"},
    pageHeading(config.modules[3], run.case_title, button),
    issue, options,
    el("section", {class: "feature evidence-summary"}, el("div", {}, el("h2", {}, "批次判定"), el("p", {}, run.result.summary)),
      el("details", {}, el("summary", {}, "证据摘要"), el("code", {}, run.digest))),
    el("dl", {class: "metrics"}, Object.entries(run.result.metrics).map(([label, value]) =>
      el("div", {}, el("dt", {}, label), el("dd", {}, cell(value))))),
    handoverOverview(run.result),
    section("业务结果明细", run.result.rows.length ? detailTable(run.result.rows) : empty("此批次无明细。")),
    section("复核签署", signing));
}

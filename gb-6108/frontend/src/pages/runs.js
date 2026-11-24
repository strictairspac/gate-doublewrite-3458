import { api } from "../api/workbench.js";
import { empty, pageHeading, section } from "../components/common/panel.js";
import { status } from "../components/common/status.js";
import { table } from "../components/common/table.js";
import { poll } from "../hooks/poll.js";
import { el } from "../utils/dom.js";
import { time } from "../utils/format.js";

function retryControl(run, signal, onQueued) {
  const issue = el("span", {class: "feedback", role: "alert"});
  const button = el("button", {
    type: "button", class: "button", "data-testid": "retry-run",
    title: "同一个批次重新执行，不会新增交班记录",
  }, "重试本批");
  button.addEventListener("click", async () => {
    button.disabled = true;
    issue.textContent = "";
    try {
      await api.retryRun(run.id);
      button.replaceWith(el("span", {class: "muted", "data-testid": "retry-queued"},
        "已重新排队，续跑同一批且不新增交班记录。"));
      onQueued?.();
      poll(onQueued, signal);
    } catch (error) {
      issue.textContent = error.message;
      button.disabled = false;
    }
  });
  return el("span", {class: "retry-control"}, button, issue);
}

function reportState(run, canRetry, signal, refresh) {
  const retry = canRetry && (run.status === "failed" || run.status === "done")
    ? retryControl(run, signal, refresh) : null;
  return el("div", {class: "stack", id: "run-state"}, status(run.status),
    el("p", {}, run.status === "queued" ? "后台正在处理，页面会自动更新。" : run.failure || run.result?.summary),
    run.result?.handover_summary
      ? el("p", {"data-testid": "run-handover-summary"}, run.result.handover_summary) : null,
    el("div", {class: "inline-actions"},
      run.status === "done" ? el("a", {class: "button", href: `#evidence/${run.id}`}, "查看业务证据") : null,
      retry));
}

export async function runsPage(config, route, signal, user) {
  const items = await api.runs();
  const latest = route.id ? await api.run(route.id, signal) : items[0];
  const canRetry = user?.role === "operator";
  async function refresh() {
    try {
      const next = await api.run(latest.id, signal);
      if (!signal.aborted) {
        detail.replaceChildren(...reportState(next, canRetry, signal, refresh).childNodes);
        const badge = document.querySelector(`[data-run="${next.id}"] .status`);
        if (badge) badge.replaceWith(status(next.status));
      }
      return next.status === "queued";
    } catch (error) {
      if (!signal.aborted) detail.append(el("p", {role: "alert"}, error.message));
      return false;
    }
  }
  const detail = latest ? reportState(latest, canRetry, signal, refresh) : empty("进入资料页，选用一个业务样本并提交执行。");
  if (latest?.status === "queued") poll(refresh, signal);
  return el("div", {class: "page"},
    pageHeading(config.modules[2], config.parameter_label),
    latest ? section(latest.case_title, detail) : detail,
    section("执行批次", items.length ? table(["资料", "编号", "参数", "状态", "提交时间"],
      items.map(run => [el("a", {"data-run": run.id, href: `#runs/${run.id}`}, run.case_title), run.id,
        run.parameter, el("span", {"data-run": run.id}, status(run.status)), time(run.created_at)]), "执行批次") :
      empty("暂无执行记录。")));
}

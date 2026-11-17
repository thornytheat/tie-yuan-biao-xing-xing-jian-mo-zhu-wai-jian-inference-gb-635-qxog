/* recon.js — 支付对账页：运行对账、续跑、关账。动作后整页重载，状态以落库为准。 */
(function () {
  function noticeNode() {
    return document.querySelector("[data-notice]");
  }

  function showNotice(text, tone) {
    const node = noticeNode();
    if (!node) {
      return;
    }
    node.textContent = text;
    node.dataset.tone = tone || "";
    node.hidden = false;
  }

  async function postJSON(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(body.message || `HTTP ${response.status}`);
    }
    return body;
  }

  async function act(button, url, payload, okText) {
    button.disabled = true;
    try {
      const body = await postJSON(url, payload);
      showNotice(okText(body), "ok");
      window.setTimeout(() => window.location.reload(), 320);
    } catch (error) {
      showNotice(String(error.message), "bad");
      button.disabled = false;
    }
  }

  function bind() {
    const run = document.querySelector("[data-action=recon-run]");
    if (run) {
      run.addEventListener("click", (event) => {
        event.preventDefault();
        act(run, "/api/recon/run", {},
            (body) => `对账完成：记账 ${body.booked} 笔，跳过 ${body.skipped} 笔`);
      });
    }
    const resume = document.querySelector("[data-action=recon-resume]");
    if (resume) {
      resume.addEventListener("click", (event) => {
        event.preventDefault();
        act(resume, "/api/recon/resume", {},
            (body) => `续跑完成：进度 ${body.processed}/${body.total}`);
      });
    }
    document.querySelectorAll("[data-action=recon-close]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.preventDefault();
        const day = button.dataset.day;
        act(button, "/api/recon/close", { day: day },
            (body) => `已关账 ${body.day}（${body.code_label}）`);
      });
    });
  }

  document.addEventListener("DOMContentLoaded", bind);
})();

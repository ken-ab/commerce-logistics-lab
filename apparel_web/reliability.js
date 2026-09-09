/* Render only the completed program comparison; preserve the original result view. */
(() => {
  const original = window.renderAgent;
  window.renderAgent = function (job) {
    original(job);
    const report = job.result?.report;
    if (job.status !== 'completed' || !report?.operation_check?.passed ||
        typeof report.revision_explanation !== 'string' || !report.revision_explanation.trim()) return;
    const panel = document.createElement('section');
    panel.className = 'version';
    panel.dataset.revisionComparison = 'true';
    const heading = document.createElement('h3');
    heading.textContent = '新旧班次与事件核对';
    const explanation = document.createElement('p');
    explanation.style.whiteSpace = 'pre-wrap';
    explanation.style.overflowWrap = 'anywhere';
    explanation.textContent = report.revision_explanation;
    const notice = document.createElement('p');
    notice.className = 'notice';
    notice.textContent = '以上说明来自已记录的方案、班次和运输事件。完整轨迹中的模型补充解释仍需另行核对。';
    panel.append(heading, explanation, notice);
    document.getElementById('agent-result').append(panel);
  };
})();

/* RogueLink Dashboard — fetch-based API helper.
 *
 * Any <form data-api="/api/..."> is intercepted: instead of navigating the
 * browser to the raw JSON endpoint the form is submitted via fetch() and
 * the response is rendered in the nearest .api-result container.
 *
 * Usage in templates:
 *   <form data-api="/api/health/check" method="post">
 *     <button type="submit">Run</button>
 *   </form>
 *   <div class="api-result" id="result-health"></div>
 *
 * For actions that should reload the page after success, add data-reload="true".
 * For scan results that need a custom renderer, add data-render="scan".
 */

(function () {
  "use strict";

  // -----------------------------------------------------------------------
  // Helpers
  // -----------------------------------------------------------------------

  function qs(sel, root) {
    return (root || document).querySelector(sel);
  }

  function ce(tag, attrs, text) {
    var el = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    if (text !== undefined) el.textContent = text;
    return el;
  }

  /** Find the nearest .api-result container for a form. */
  function findResultTarget(form) {
    // Explicit target via data-target="#id"
    var tid = form.getAttribute("data-target");
    if (tid) return qs(tid);
    // Next sibling .api-result
    var sib = form.nextElementSibling;
    while (sib) {
      if (sib.classList && sib.classList.contains("api-result")) return sib;
      sib = sib.nextElementSibling;
    }
    // Parent's next sibling
    var parent = form.parentElement;
    if (parent) {
      sib = parent.nextElementSibling;
      while (sib) {
        if (sib.classList && sib.classList.contains("api-result")) return sib;
        sib = sib.nextElementSibling;
      }
    }
    // Fallback: create one after the form
    var div = ce("div", { class: "api-result" });
    form.parentNode.insertBefore(div, form.nextSibling);
    return div;
  }

  // -----------------------------------------------------------------------
  // Renderers
  // -----------------------------------------------------------------------

  function renderLoading(target) {
    target.className = "api-result loading";
    target.innerHTML = '<span class="spinner"></span> Working…';
  }

  function renderSuccess(target, data) {
    target.className = "api-result success";
    if (typeof data === "string") {
      target.textContent = data;
      return;
    }
    // Build a small key-value display
    var html = "";
    if (data.ok === true) html += '<p class="api-ok">✓ OK</p>';
    if (data.ok === false) html += '<p class="api-err">✗ Failed</p>';
    if (data.error) html += '<p class="api-err">' + escHtml(data.error) + "</p>";
    if (data.message) html += "<p>" + escHtml(data.message) + "</p>";
    // Show select keys
    var skip = new Set(["ok", "error", "message", "raw", "output", "public_targets",
      "dns_targets", "gateway_ping", "dns_servers", "wan_signal"]);
    Object.keys(data).forEach(function (k) {
      if (skip.has(k)) return;
      var v = data[k];
      if (v === null || v === undefined) return;
      if (typeof v === "object") {
        html += "<p><strong>" + escHtml(k) + ":</strong> <code>" + escHtml(JSON.stringify(v)) + "</code></p>";
      } else {
        html += "<p><strong>" + escHtml(k) + ":</strong> " + escHtml(String(v)) + "</p>";
      }
    });
    target.innerHTML = html || "<p>Done.</p>";
  }

  function renderError(target, message) {
    target.className = "api-result error";
    target.innerHTML = '<p class="api-err">✗ ' + escHtml(message) + "</p>";
  }

  function escHtml(s) {
    var d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  /** Render Wi-Fi scan results as a table. */
  function renderScanTable(target, data) {
    target.className = "api-result success";
    var networks = data.networks || [];
    if (!networks.length) {
      target.innerHTML = "<p>No networks found.</p>";
      return;
    }
    var cols = ["SSID", "BSSID", "Signal dBm", "Quality", "Channel", "Band", "Security", "Interface", "Actions"];
    var table = ce("table");
    var thead = ce("thead");
    var tr = ce("tr");
    cols.forEach(function (c) { tr.appendChild(ce("th", null, c)); });
    thead.appendChild(tr);
    table.appendChild(thead);
    var tbody = ce("tbody");
    networks.forEach(function (n) {
      var row = ce("tr");
      row.appendChild(ce("td", null, n.ssid || ""));
      row.appendChild(ce("td", null, n.bssid || ""));
      var sig = n.signal_dbm;
      row.appendChild(ce("td", null, sig != null ? Math.round(sig).toString() : "—"));
      row.appendChild(ce("td", null, n.quality || "—"));
      row.appendChild(ce("td", null, n.channel != null ? String(n.channel) : "—"));
      row.appendChild(ce("td", null, n.band || "—"));
      row.appendChild(ce("td", null, n.security || "—"));
      row.appendChild(ce("td", null, n.iface || "—"));
      // Actions: save button
      var actTd = ce("td");
      var saveBtn = ce("button", { class: "btn-sm", "data-ssid": n.ssid || "" }, "Save");
      saveBtn.addEventListener("click", function () { openSaveDialog(n); });
      actTd.appendChild(saveBtn);
      row.appendChild(actTd);
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    target.innerHTML = "";
    var heading = ce("p", null, networks.length + " network(s) found on " + (data.iface || "—"));
    heading.style.marginBottom = "8px";
    target.appendChild(heading);
    target.appendChild(table);
  }

  /** Render health check result as a structured card. */
  function renderHealth(target, data) {
    target.className = "api-result success";
    var html = "";
    var color = { excellent: "#4ade80", good: "#4ade80", partial: "#fbbf24", weak: "#fbbf24", unstable: "#fbbf24", offline: "#f87171" };
    var status = data.status || data.overall || "unknown";
    var c = color[status] || "#d6dde5";
    html += '<p><strong>Overall:</strong> <span style="color:' + c + '">' + escHtml(status) + "</span></p>";
    if (data.reason) html += "<p><strong>Reason:</strong> " + escHtml(data.reason) + "</p>";
    var s = data.summary || {};
    if (s.rtt_ms != null) html += "<p><strong>RTT:</strong> " + s.rtt_ms + " ms</p>";
    if (s.packet_loss_pct != null) html += "<p><strong>Packet loss:</strong> " + s.packet_loss_pct + "%</p>";
    if (s.gateway) html += "<p><strong>Gateway:</strong> " + escHtml(s.gateway) + "</p>";
    if (s.wan_iface) html += "<p><strong>WAN iface:</strong> " + escHtml(s.wan_iface) + "</p>";
    if (s.signal_dbm != null) html += "<p><strong>Signal:</strong> " + s.signal_dbm + " dBm</p>";
    if (data.dns_ok !== undefined) html += "<p><strong>DNS:</strong> " + (data.dns_ok ? "OK" : "Failing") + "</p>";
    if (data.management_internet !== undefined)
      html += "<p><strong>Management Internet:</strong> " + (data.management_internet ? "OK" : "No") + "</p>";
    if (data.wan_status) html += "<p><strong>WAN status:</strong> " + escHtml(data.wan_status) + "</p>";
    if (data.duration_s != null) html += '<p class="muted">Checked in ' + data.duration_s + "s</p>";
    target.innerHTML = html;
  }

  /** Render speedtest result. */
  function renderSpeedtest(target, data) {
    target.className = "api-result success";
    var html = "";
    if (data.ok === false) {
      html += '<p class="api-err">✗ ' + escHtml(data.error || "Speed test failed") + "</p>";
    } else {
      html += "<p><strong>Download:</strong> " + (data.download_mbps || "—") + " Mbps</p>";
      html += "<p><strong>Upload:</strong> " + (data.upload_mbps || "—") + " Mbps</p>";
      html += "<p><strong>Ping:</strong> " + (data.ping_ms || "—") + " ms</p>";
      if (data.server_name) html += "<p><strong>Server:</strong> " + escHtml(data.server_name) + "</p>";
    }
    target.innerHTML = html;
  }

  /** Render WAN scan results table. */
  function renderWanScan(target, data) {
    target.className = "api-result success";
    var networks = data.networks || [];
    if (!networks.length) {
      target.innerHTML = "<p>No networks found.</p>";
      return;
    }
    var table = ce("table");
    var thead = ce("thead");
    var tr = ce("tr");
    ["SSID", "BSSID", "Channel", "Signal", "Encryption"].forEach(function (c) { tr.appendChild(ce("th", null, c)); });
    thead.appendChild(tr);
    table.appendChild(thead);
    var tbody = ce("tbody");
    networks.forEach(function (n) {
      var row = ce("tr");
      row.appendChild(ce("td", null, n.ssid || ""));
      row.appendChild(ce("td", null, n.bssid || ""));
      row.appendChild(ce("td", null, n.channel != null ? String(n.channel) : "—"));
      row.appendChild(ce("td", null, n.signal != null ? String(n.signal) : "—"));
      row.appendChild(ce("td", null, n.encryption || "—"));
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    target.innerHTML = "";
    target.appendChild(ce("p", null, networks.length + " network(s) found."));
    target.appendChild(table);
  }

  function openSaveDialog(network) {
    var ssid = network.ssid || "";
    var psk = prompt("PSK for "" + ssid + "" (leave empty for open):", "");
    if (psk === null) return; // cancelled
    var note = prompt("Note (optional):", "");
    if (note === null) note = "";
    var body = new FormData();
    body.append("ssid", ssid);
    body.append("psk", psk);
    body.append("note", note);
    fetch("/api/networks/saved", { method: "POST", body: body })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.ok) alert("Saved: " + ssid);
        else alert("Error: " + (d.error || "unknown"));
      })
      .catch(function (e) { alert("Network error: " + e.message); });
  }

  // -----------------------------------------------------------------------
  // Custom renderer registry
  // -----------------------------------------------------------------------

  var renderers = {
    scan: renderScanTable,
    health: renderHealth,
    speedtest: renderSpeedtest,
    wanscan: renderWanScan,
  };

  // -----------------------------------------------------------------------
  // Form interception
  // -----------------------------------------------------------------------

  function handleSubmit(ev) {
    ev.preventDefault();
    var form = ev.target;
    var url = form.getAttribute("data-api");
    if (!url) return;
    var method = (form.method || "POST").toUpperCase();
    var target = findResultTarget(form);
    var renderName = form.getAttribute("data-render");
    var reload = form.getAttribute("data-reload") === "true";

    renderLoading(target);

    // Disable submit buttons during request
    var btns = form.querySelectorAll('button[type="submit"], button:not([type])');
    btns.forEach(function (b) { b.disabled = true; });

    var opts = { method: method };
    if (method === "POST" || method === "PATCH" || method === "PUT") {
      opts.body = new FormData(form);
    } else if (method === "GET") {
      var params = new URLSearchParams(new FormData(form)).toString();
      if (params) url += (url.indexOf("?") === -1 ? "?" : "&") + params;
    }

    fetch(url, opts)
      .then(function (resp) {
        if (!resp.ok) {
          return resp.json().catch(function () { return { error: "HTTP " + resp.status }; }).then(function (d) {
            throw new Error(d.detail || d.error || "HTTP " + resp.status);
          });
        }
        return resp.json();
      })
      .then(function (data) {
        if (renderName && renderers[renderName]) {
          renderers[renderName](target, data);
        } else {
          renderSuccess(target, data);
        }
        if (reload) {
          setTimeout(function () { location.reload(); }, 1200);
        }
      })
      .catch(function (err) {
        renderError(target, err.message || "Request failed");
      })
      .finally(function () {
        btns.forEach(function (b) { b.disabled = false; });
      });
  }

  // -----------------------------------------------------------------------
  // Programmatic API call (for use from other scripts if needed)
  // -----------------------------------------------------------------------

  window.runApiAction = function (endpoint, payload, targetSelector) {
    var target = qs(targetSelector) || document.body;
    renderLoading(target);
    var opts = { method: "POST" };
    if (payload) {
      var fd = new FormData();
      Object.keys(payload).forEach(function (k) { fd.append(k, payload[k]); });
      opts.body = fd;
    }
    return fetch(endpoint, opts)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        renderSuccess(target, data);
        return data;
      })
      .catch(function (err) {
        renderError(target, err.message);
      });
  };

  // -----------------------------------------------------------------------
  // DELETE method helper — forms can't use DELETE natively
  // -----------------------------------------------------------------------

  function handleDelete(ev) {
    ev.preventDefault();
    var btn = ev.currentTarget;
    var url = btn.getAttribute("data-delete");
    if (!url) return;
    if (!confirm("Are you sure?")) return;
    var target = findResultTarget(btn.closest("form") || btn.parentElement);
    renderLoading(target);
    fetch(url, { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        renderSuccess(target, data);
        if (data.ok) setTimeout(function () { location.reload(); }, 1000);
      })
      .catch(function (err) { renderError(target, err.message); });
  }

  // -----------------------------------------------------------------------
  // Init on DOMContentLoaded
  // -----------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    // Attach to all forms with data-api
    document.querySelectorAll("form[data-api]").forEach(function (form) {
      form.addEventListener("submit", handleSubmit);
    });
    // Attach delete buttons
    document.querySelectorAll("[data-delete]").forEach(function (btn) {
      btn.addEventListener("click", handleDelete);
    });
  });
})();

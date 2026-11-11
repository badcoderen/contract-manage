/* 工程合同・费用・发票管理系统 —— 前端交互 */
(function () {
  "use strict";

  function round2(x) { return Math.round((Number(x) || 0) * 100) / 100; }

  function fmt(x) {
    var n = Number(x) || 0;
    return n.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  /* 必须放在模块作用域：initSettle 与 initDetails 都要用它。
     以前它写在 initSettle 里，于是「没有核销区的表单」（如报价单）
     调用 initDetails 时直接 ReferenceError —— 明细的「+ 添加一行」
     就彻底没反应了（点了没反应，也没有任何提示）。 */
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* ------------------------------------------------ 价税合计 / 税率 自动计算 */
  function initTaxCalc(form) {
    var gName = form.dataset.calcGross;
    var rName = form.dataset.calcRate;
    var nName = form.dataset.calcNet;
    var tName = form.dataset.calcTax;
    if (!gName) return;

    var gEl = form.querySelector('[name="' + gName + '"]');
    var rEl = form.querySelector('[name="' + rName + '"]');
    var nEl = form.querySelector('[name="' + nName + '"]');
    var tEl = form.querySelector('[name="' + tName + '"]');
    if (!gEl || !rEl || !nEl || !tEl) return;

    function calc() {
      var gross = round2(gEl.value);
      var rate = Number(rEl.value) || 0;
      var net = rate > -1 ? round2(gross / (1 + rate)) : gross;
      nEl.value = net.toFixed(2);
      tEl.value = round2(gross - net).toFixed(2);
    }
    ["input", "change"].forEach(function (ev) {
      gEl.addEventListener(ev, calc);
      rEl.addEventListener(ev, calc);
    });
    calc();
  }

  /* ------------------------------------------------ 付款 ↔ 发票 核销编辑器 */
  function initSettle() {
    var dataEl = document.getElementById("invoiceData");
    if (!dataEl) return;
    var invoices = JSON.parse(dataEl.textContent || "[]");
    var existing = JSON.parse(
      (document.getElementById("settleRows") || {}).textContent || "[]");
    var form = document.getElementById("mainform");
    var tbody = document.querySelector("#settleTable tbody");
    if (!tbody) return;

    var byId = {};
    invoices.forEach(function (i) { byId[String(i.id)] = i; });

    function optionHtml(selected) {
      var out = ['<option value="">— 请选择发票 —</option>'];
      invoices.forEach(function (i) {
        out.push('<option value="' + i.id + '"' +
          (String(selected) === String(i.id) ? " selected" : "") + ">" +
          escapeHtml(i.label) + "</option>");
      });
      return out.join("");
    }

    function addRow(row) {
      row = row || {};
      var tr = document.createElement("tr");
      // data-label 是给窄屏用的：手机上这张表会变成"一票一张卡片"（见 app.css），
      // 表头藏起来，每格靠 ::before 把标签显出来。桌面端完全不受影响。
      tr.innerHTML =
        '<td data-label="发票"><select name="s_invoice_id">' + optionHtml(row.invoice_id) +
        "</select>" +
        // 下拉里那串字很容易被截断（窄屏尤其），这里把「公司 · 日期 · 发票号」完整写一行，
        // 保证不管屏幕多窄都能看全
        '<div class="settle-inv"></div>' +
        '<div class="settle-remain"></div></td>' +
        '<td class="gross num" data-label="价税合计"></td>' +
        '<td class="num" data-label="核销金额">' +
        '<input type="number" step="0.01" name="s_amount"></td>' +
        '<td class="num" data-label="核销比例">' +
        '<input type="number" step="0.0001" name="s_ratio"></td>' +
        '<td data-label="备注"><input type="text" name="s_memo"></td>' +
        '<td data-label=""><button class="btn btn-sm btn-danger" type="button">删</button></td>';
      tbody.appendChild(tr);

      var sel = tr.querySelector('[name="s_invoice_id"]');
      var amtEl = tr.querySelector('[name="s_amount"]');
      var ratEl = tr.querySelector('[name="s_ratio"]');
      var memoEl = tr.querySelector('[name="s_memo"]');
      var grossCell = tr.querySelector(".gross");
      var remainEl = tr.querySelector(".settle-remain");
      var invEl = tr.querySelector(".settle-inv");

      if (row.amount !== undefined && row.amount !== null && row.amount !== "") {
        amtEl.value = row.amount;
      }
      if (row.ratio !== undefined && row.ratio !== null && row.ratio !== "") {
        ratEl.value = row.ratio;
      }
      memoEl.value = row.memo || "";

      function current() {
        return byId[String(sel.value)] || null;
      }

      function refreshInfo() {
        var inv = current();
        if (!inv) {
          grossCell.textContent = "";
          invEl.textContent = "";
          remainEl.textContent = "";
          remainEl.className = "settle-remain";
          return;
        }
        grossCell.textContent = fmt(inv.gross);
        invEl.textContent = [inv.unit, inv.date, inv.no]
          .filter(function (x) { return x; }).join(" · ");
        var base = Number(inv.remaining);
        var mine = Number(amtEl.value) || 0;
        var left = round2(base - mine);
        remainEl.textContent = "剩余可核销 " + fmt(base) +
          "　｜　填入后剩 " + fmt(left);
        remainEl.className = "settle-remain" + (left < -0.01 ? " over" : "");
      }

      sel.addEventListener("change", function () {
        var inv = current();
        if (inv && (!amtEl.value || Number(amtEl.value) === 0)) {
          amtEl.value = round2(inv.remaining).toFixed(2);
          ratEl.value = inv.gross > 0 ? (Number(amtEl.value) / inv.gross).toFixed(4) : "";
        }
        refreshInfo();
        updateSummary();
      });

      amtEl.addEventListener("input", function () {
        var inv = current();
        if (inv && inv.gross > 0) {
          ratEl.value = (round2(amtEl.value) / inv.gross).toFixed(4);
        }
        refreshInfo();
        updateSummary();
      });

      ratEl.addEventListener("input", function () {
        var inv = current();
        var r = Number(ratEl.value) || 0;
        if (inv && inv.gross > 0 && r > 0) {
          amtEl.value = round2(inv.gross * r).toFixed(2);
        }
        refreshInfo();
        updateSummary();
      });

      tr.querySelector("button").addEventListener("click", function () {
        tr.parentNode.removeChild(tr);
        updateSummary();
      });

      refreshInfo();
      updateSummary();
      return tr;
    }

    function updateSummary() {
      var amtInput = form.querySelector('[name="amount"]');
      var pay = amtInput ? round2(amtInput.value) : 0;
      var sum = 0;
      tbody.querySelectorAll('[name="s_amount"]').forEach(function (el) {
        sum += round2(el.value);
      });
      sum = round2(sum);
      var el1 = document.getElementById("payAmount");
      var el2 = document.getElementById("paySettled");
      var el3 = document.getElementById("payRest");
      if (el1) el1.textContent = fmt(pay);
      if (el2) el2.textContent = fmt(sum);
      if (el3) {
        var rest = round2(pay - sum);
        el3.textContent = fmt(rest);
        el3.style.color = rest < -0.01 ? "#c0392b" : "";
      }
    }

    window.addSettleRow = function () { addRow({}); };
    window.updateSummary = updateSummary;

    existing.forEach(addRow);
    if (!existing.length) addRow();

    var amtInput = form.querySelector('[name="amount"]');
    if (amtInput) {
      amtInput.addEventListener("input", updateSummary);
    }
    updateSummary();
  }

  /* ------------------------------------------------ 时长 / 天数 / 补贴 自动计算
     加班：选完起止时间即时算出加班时长（支持跨夜）
     请假：选完起止日期算天数，并折合小时
     出差：选完出发/回程算天数，再按每日标准算补贴
     规则：用户手工改过的数字不会被自动覆盖；清空该数字即可恢复自动计算。 */
  function initAutoCalc(form) {
    var cfg;
    try { cfg = JSON.parse(form.dataset.autoCalc || "null"); } catch (e) { return; }
    if (!cfg) return;
    var startEl = form.querySelector('[name="' + cfg.start + '"]');
    var endEl = form.querySelector('[name="' + cfg.end + '"]');
    if (!startEl || !endEl) return;
    var params = {};
    try { params = JSON.parse(form.dataset.params || "{}"); } catch (e) { params = {}; }

    function toMinutes(s) {
      var m = /^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})/.exec(s || "");
      return m ? (parseInt(m[1], 10) * 60 + parseInt(m[2], 10)) : null;
    }

    // 起点值：时间差换算成小时，日期差换算成天数（含首尾）
    function baseValue() {
      var s = startEl.value, e = endEl.value;
      if (!s || !e) return null;
      if (cfg.kind === "time") {
        var a = toMinutes(s), b = toMinutes(e);
        if (a === null || b === null) return null;
        if (b < a) b += 24 * 60;                        // 跨夜
        return Math.round((b - a) / 60 * 100) / 100;
      }
      var d1 = new Date(s), d2 = new Date(e);
      if (isNaN(d1.getTime()) || isNaN(d2.getTime())) return null;
      var n = Math.round((d2 - d1) / 86400000) + 1;
      return n > 0 ? n : null;
    }

    function apply() {
      if (!startEl.value && !endEl.value) return;       // 都还没填，别乱写 0
      var base = baseValue();
      (cfg.targets || []).forEach(function (t) {
        var el = form.querySelector('[name="' + t.field + '"]');
        if (!el) return;
        var v;
        if (base === null) {
          v = 0;                                        // 只填一半（如回程未定）
        } else {
          v = base;
          if (t.mul_param) v = base * (Number(params[t.mul_param]) || 0);
          if (t.mul_field) {
            var src = form.querySelector('[name="' + t.mul_field + '"]');
            v = base * (Number(src && src.value) || 0);
          }
        }
        v = Math.round(v * 100) / 100;
        // 用户手工改过的值不覆盖：只有当前值仍等于「上次自动算出的值」时才写
        if (el.value !== "" && el.value !== el.dataset.autoValue) return;
        el.value = v;
        el.dataset.autoValue = String(v);
      });
    }

    // 先把现有值记成「自动值」，再挂监听。
    // 注意：初始化时不调用 apply()，否则编辑既有记录时会把存好的数字改掉。
    (cfg.targets || []).forEach(function (t) {
      var el = form.querySelector('[name="' + t.field + '"]');
      if (el) el.dataset.autoValue = el.value;
    });
    [startEl, endEl].forEach(function (el) {
      ["change", "input", "blur"].forEach(function (ev) {
        el.addEventListener(ev, apply);
      });
    });
    (cfg.targets || []).forEach(function (t) {
      if (!t.mul_field) return;
      var src = form.querySelector('[name="' + t.mul_field + '"]');
      if (src) src.addEventListener("input", apply);
    });
  }

  /* ------------------------------------------------ 通用明细表编辑器
     报价明细 / 合同清单 / 送货明细 / 付款·费用对应清单项 共用。
     列定义来自后端 meta（<script id="detailSpecs">），字段名 d_<key>_<列>。 */
  function jsonOf(id, dflt) {
    var el = document.getElementById(id);
    if (!el) return dflt;
    try { return JSON.parse(el.textContent || "null") || dflt; }
    catch (e) { return dflt; }
  }

  function initDetails() {
    var specs = jsonOf("detailSpecs", []);
    if (!specs.length) return;
    var brands = jsonOf("brandData", []);
    var models = jsonOf("modelData", []);
    var units = jsonOf("unitData", []);
    var citems = jsonOf("contractItemData", []);
    var form = document.getElementById("mainform");
    var masterKey = jsonOf("masterContractField", "contract_id");

    // 下拉候选项：一次建好，插入页面供各行的 input[list] 用
    function ensureDatalists() {
      var html = '<datalist id="dlBrand">';
      brands.forEach(function (b) { html += '<option value="' + escapeHtml(b.name) + '">'; });
      html += '</datalist><datalist id="dlModel">';
      models.forEach(function (m) {
        var lb = [m.brand, m.name].filter(Boolean).join(" ");
        html += '<option value="' + escapeHtml(m.name) + '" label="' + escapeHtml(lb) + '">';
      });
      html += '</datalist><datalist id="dlUnit">';
      units.forEach(function (u) { html += '<option value="' + escapeHtml(u) + '">'; });
      html += '</datalist>';
      var box = document.createElement("div");
      box.innerHTML = html;
      document.body.appendChild(box);
    }
    ensureDatalists();

    var modelByName = {};
    models.forEach(function (m) { modelByName[m.name] = m; });
    var citemById = {};
    citems.forEach(function (it) { citemById[String(it.id)] = it; });

    // 明细行里的【所属合同】下拉用
    var contractPick = jsonOf("contractPickData", []);

    // 清单项按合同分组：一个对私单可能关联多张合同，
    // 几十项平铺在一起根本认不出哪项是哪张合同的
    var citemGroups = [];
    (function () {
      var idx = {};
      citems.forEach(function (it) {
        var k = String(it.contract_id);
        if (!idx[k]) {
          var lb = (String(it.contract_code || "").trim() + " " +
                    String(it.contract_name || "").trim()).trim();
          idx[k] = { contract_id: it.contract_id, label: lb || ("合同 " + k),
                     items: [] };
          citemGroups.push(idx[k]);
        }
        idx[k].items.push(it);
      });
    })();

    // 表头上「关联合同」勾了哪些（多选）
    function headerContracts() {
      var out = [];
      document.querySelectorAll('input[name="contract_ids"]:checked').forEach(function (x) {
        out.push(String(x.value));
      });
      return out;
    }

    function currentContract() {
      var el = form ? form.querySelector('[name="' + masterKey + '"]') : null;
      return el ? el.value : "";
    }

    function citemOptions(row) {
      var keep = row && row.contract_item_id ? String(row.contract_item_id) : "";
      var rc = row && row.row_contract_id ? String(row.row_contract_id) : "";
      var mc = currentContract();
      // 先看这一行自己的所属合同；没有就看主表的合同（付款/费用那种）；
      // 都没有就看表头勾选的合同；再没有就全给（按合同分组）
      var scope = rc ? [rc] : (mc ? [String(mc)] : headerContracts());
      var out = ['<option value="">— 请选择合同清单项 —</option>'];
      citemGroups.forEach(function (g) {
        if (scope.length && scope.indexOf(String(g.contract_id)) < 0) return;
        var body = g.items.map(function (it) {
          return '<option value="' + it.id + '"' +
            (String(it.id) === keep ? " selected" : "") + ">" +
            escapeHtml(it.label) + "</option>";
        }).join("");
        if (!body) return;
        out.push('<optgroup label="' + escapeHtml(g.label) + '">' + body + "</optgroup>");
      });
      return out.join("");
    }

    // 重画某一行的清单项下拉（所属合同变了 / 表头勾选变了都要重画）
    function refreshRowCitem(tr, spec) {
      var sel = tr.querySelector(".dt-citem");
      if (!sel) return;
      var cEl = tr.querySelector(".dt-contract");
      var keep = sel.value;
      sel.innerHTML = citemOptions({ contract_item_id: keep,
                                     row_contract_id: cEl ? cEl.value : "" });
      // 原来选的清单项不属于新合同 → 这里会自然落空（下拉回到"请选择"）
      sel.value = keep || "";
    }

    function refreshAllCitems() {
      document.querySelectorAll(".dt-citem").forEach(function (sel) {
        var tr = sel.closest("tr");
        var cEl = tr ? tr.querySelector(".dt-contract") : null;
        var keep = sel.value;
        sel.innerHTML = citemOptions({ contract_item_id: keep,
                                       row_contract_id: cEl ? cEl.value : "" });
        sel.value = keep || "";
      });
    }

    function cellHtml(spec, col, row) {
      var name = "d_" + spec.key + "_" + col.name;
      var val = row && row[col.name] !== null && row[col.name] !== undefined
        ? String(row[col.name]) : "";
      var cls = "dt-cell";
      if (col.type === "money" || col.type === "number" || col.type === "pct") {
        cls += " num";
      }
      // 新增行带出列的默认值（如「含税」「13%」），省得每行都点一遍
      if (val === "" && col.default !== undefined && col.default !== null) {
        val = String(col.default);
      }
      if (col.type === "hidden") {
        return '<input type="hidden" name="' + name + '" value="' + escapeHtml(val) + '">';
      }
      if (col.type === "contract") {
        // 这一行属于哪张合同（可留空＝共同成本）
        var co = ['<select name="' + name + '" class="' + cls + ' dt-contract">',
                  '<option value="">（共同成本）</option>'];
        contractPick.forEach(function (c) {
          var v = String(c[0]);
          co.push('<option value="' + escapeHtml(v) + '"' +
            (val === v ? " selected" : "") + ">" + escapeHtml(String(c[1])) +
            "</option>");
        });
        co.push("</select>");
        return co.join("");
      }
      if (col.type === "select") {
        var out = ['<select name="' + name + '" class="' + cls + '">'];
        if (!col.required) out.push('<option value=""></option>');
        (col.options || []).forEach(function (o) {
          var s = String(o);
          out.push('<option value="' + escapeHtml(s) + '"' +
            (val === s ? " selected" : "") + ">" + escapeHtml(s) + "</option>");
        });
        out.push("</select>");
        return out.join("");
      }
      if (col.type === "contractitem") {
        return '<select name="' + name + '" class="' + cls + ' dt-citem">' +
          citemOptions(row) + "</select>";
      }
      if (col.type === "brand") {
        return '<input type="text" list="dlBrand" class="' + cls + ' dt-brand" name="' +
          name + '" value="' + escapeHtml(val) + '" placeholder="品牌">';
      }
      if (col.type === "model") {
        return '<input type="text" list="dlModel" class="' + cls + ' dt-model" name="' +
          name + '" value="' + escapeHtml(val) + '" placeholder="型号">';
      }
      if (col.type === "unit") {
        return '<input type="text" list="dlUnit" class="' + cls + ' dt-unit" name="' +
          name + '" value="' + escapeHtml(val) + '" placeholder="单位">';
      }
      if (col.type === "pct") {
        // 输入框里只放数字（13），% 用旁边的 span 显示；库里存的是 "13%"
        var pv = val === "" ? "" : String(val).replace("%", "").trim();
        return '<span class="dt-pct-wrap"><input type="number" step="0.01" min="0" ' +
          'max="100" class="' + cls + '" name="' + name + '" value="' +
          escapeHtml(pv) + '" placeholder="0"><span class="dt-pct">%</span></span>';
      }
      if (col.type === "money" || col.type === "number") {
        return '<input type="number" step="0.01" class="' + cls + '" name="' + name +
          '" value="' + (val === "" ? "" : val) + '"' +
          (col.ro ? " readonly" : "") + (col.calc ? ' data-calc="' + col.calc + '"' : "") +
          (col.expr ? ' data-expr="' + escapeHtml(col.expr) + '"' : "") + ">";
      }
      return '<input type="text" class="' + cls + '" name="' + name + '" value="' +
        escapeHtml(val) + '"' + (col.ph ? ' placeholder="' + escapeHtml(col.ph) + '"' : "") + ">";
    }

    function recalcRow(tr, spec) {
      spec.cols.forEach(function (col) {
        if (!col.calc) return;
        var el = tr.querySelector('[name="d_' + spec.key + "_" + col.name + '"]');
        if (!el) return;
        var a = tr.querySelector('[name="d_' + spec.key + "_" + col.calc.split("*")[0].trim() + '"]');
        var b = tr.querySelector('[name="d_' + spec.key + "_" + col.calc.split("*")[1].trim() + '"]');
        el.value = round2((Number(a && a.value) || 0) * (Number(b && b.value) || 0)).toFixed(2);
      });
    }

    function recalcSum(box) {
      var spec = box.spec;
      (spec.sum || []).forEach(function (col) {
        var total = 0;
        box.tbody.querySelectorAll('[name="d_' + spec.key + "_" + col + '"]').forEach(
          function (el) { total += Number(el.value) || 0; });
        var out = box.sumBox ? box.sumBox.querySelector('[data-sum="' + col + '"]') : null;
        if (out) out.textContent = fmt(round2(total));
        // 对私计算的结算看板：同样的四个数，跟着明细实时走
        var bk = { income: "income", cost_amount: "cost",
                   tax_amount: "profit", payable: "payable" }[col];
        if (bk) {
          var bEl = document.querySelector('[data-board="' + bk + '"]');
          if (bEl) bEl.textContent = fmt(round2(total));
        }
      });
    }

    /* "13%" → 0.13（和 services.pct_value 保持同一套口径） */
    /* "13%" / 13 → 0.13（与 services.pct_value 同一套口径）：
       一律按百分点 —— 填 0.5 就是 0.5%，不是 50%。
       两种解释混在一起，读错一次就是 100 倍的金额差，所以只认一种。 */
    function pctOf(v) {
      var t = String(v == null ? "" : v).trim().replace("％", "%");
      if (!t) return 0;
      if (t.charAt(t.length - 1) === "%") t = t.slice(0, -1).trim();
      return (Number(t) || 0) / 100;
    }

    /* 派生列公式（meta 里的 col.expr，如 扣税 = income * pct(tax_rate)）：

       pct(...) 必须**先**替换 —— "13%" 不是数字，先按列名换会变成 0。
       换完只剩数字与运算符，再用白名单字符集卡一道才交给 Function，
       所以即便表达式来自配置也执行不了别的东西。 */
    /* 极小的四则运算求值器：只认 数字 + - * / ( )。

       为什么不用 new Function / eval：页面有 CSP
       （script-src 'self' 'unsafe-inline'，没有 unsafe-eval），
       浏览器会把 eval / Function **直接拦掉** —— 表现是派生列全变 0，
       而且 pageerror 都抓不到（被 try 吞了），极难查。所以自己解析。 */
    function calcArith(src) {
      var txt = String(src), i = 0;
      function skip() { while (i < txt.length && txt.charAt(i) === " ") i++; }
      function num() {
        skip();
        var st = i;
        while (i < txt.length && /[0-9.]/.test(txt.charAt(i))) i++;
        return st === i ? 0 : (Number(txt.slice(st, i)) || 0);
      }
      function factor() {
        skip();
        var c = txt.charAt(i);
        if (c === "-") { i++; return -factor(); }
        if (c === "+") { i++; return factor(); }
        if (c === "(") {
          i++;
          var v = sum();
          skip();
          if (txt.charAt(i) === ")") i++;
          return v;
        }
        return num();
      }
      function term() {
        var v = factor(), c;
        for (;;) {
          skip();
          c = txt.charAt(i);
          if (c === "*") { i++; v = v * factor(); }
          else if (c === "/") { i++; var d = factor(); v = d ? v / d : 0; }
          else return v;
        }
      }
      function sum() {
        var v = term(), c;
        for (;;) {
          skip();
          c = txt.charAt(i);
          if (c === "+") { i++; v = v + term(); }
          else if (c === "-") { i++; v = v - term(); }
          else return v;
        }
      }
      return sum();
    }

    function recalcExpr(tr, spec) {
      function get(name) {
        var x = tr.querySelector('[name="d_' + spec.key + "_" + name + '"]');
        return x ? x.value : "";
      }
      (spec.cols || []).forEach(function (col) {
        if (!col.expr) return;
        var el = tr.querySelector('[name="d_' + spec.key + "_" + col.name + '"]');
        if (!el) return;
        var js = String(col.expr)
          .replace(/pct\s*\(\s*([A-Za-z_][A-Za-z_0-9]*)\s*\)/g,
                   function (m, c) { return "(" + pctOf(get(c)) + ")"; })
          .replace(/[A-Za-z_][A-Za-z_0-9]*/g,
                   function (id) { return "(" + (Number(get(id)) || 0) + ")"; });
        el.value = round2(calcArith(js)).toFixed(2);
      });
    }

    /* 选中「对应合同清单项」后，把它的字段带到本行的其他列
       （meta 里的 col.fills = {本行列名: 清单项字段名}）。
       换清单项就是一次明确的选择动作，直接覆盖，避免"换了项目金额还是旧的"。 */
    function applyRowFills(tr, spec) {
      (spec.cols || []).forEach(function (col) {
        if (col.type !== "contractitem" || !col.fills) return;
        var sel = tr.querySelector('[name="d_' + spec.key + "_" + col.name + '"]');
        if (!sel || !sel.value) return;
        var src = citemById[String(sel.value)];
        if (!src) return;
        Object.keys(col.fills).forEach(function (target) {
          var el = tr.querySelector('[name="d_' + spec.key + "_" + target + '"]');
          if (!el) return;
          var v = src[col.fills[target]];
          el.value = (v === null || v === undefined) ? "" : String(v);
        });
      });
    }

    /* 明细行的列联动：meta 里 col.sets = {"不含税": {"tax_rate": "0%"}}
       表示「这一列选到不含税时，把同行的 tax_rate 钉成 0%」。
       反向不定义就不自动 —— 改回含税时保留当前税点。 */
    function applyRowLinks(tr, spec) {
      (spec.cols || []).forEach(function (col) {
        if (!col.sets) return;
        var src = tr.querySelector('[name="d_' + spec.key + "_" + col.name + '"]');
        if (!src) return;
        var target = col.sets[src.value];
        if (!target) return;
        Object.keys(target).forEach(function (name) {
          var el = tr.querySelector('[name="d_' + spec.key + "_" + name + '"]');
          if (el && el.value !== target[name]) el.value = target[name];
        });
      });
    }

    function wireRow(tr, box) {
      var spec = box.spec;
      // 型号选好后自动带出品牌 / 参数 / 单位 / 参考单价
      var mEl = tr.querySelector(".dt-model");
      if (mEl) {
        mEl.addEventListener("change", function () {
          var m = modelByName[mEl.value.trim()];
          if (!m) return;
          var bEl = tr.querySelector(".dt-brand");
          var sEl = tr.querySelector('[name="d_' + spec.key + '_spec"]');
          var uEl = tr.querySelector(".dt-unit");
          var pEl = tr.querySelector('[name="d_' + spec.key + '_price"]');
          if (bEl && !bEl.value) bEl.value = m.brand || "";
          if (sEl && !sEl.value) sEl.value = m.spec || "";
          if (uEl && !uEl.value) uEl.value = m.unit || "";
          if (pEl && !Number(pEl.value) && Number(m.price)) pEl.value = m.price;
          recalcRow(tr, spec);
          recalcSum(box);
        });
      }
      tr.addEventListener("input", function () {
        recalcRow(tr, spec);
        recalcExpr(tr, spec);
        recalcSum(box);
      });
      // 下拉一变：只有「合同清单项」自己变了才带出字段（换项目才该覆盖），
      // 否则用户手工调完税点、再点别的下拉就会被清单项的原值冲掉。
      tr.addEventListener("change", function (ev) {
        if (!ev.target || ev.target.tagName !== "SELECT") return;
        if (ev.target.classList.contains("dt-contract")) {
          refreshRowCitem(tr, spec);      // 换了合同 → 清单项范围跟着换
        }
        if (ev.target.classList.contains("dt-citem")) {
          applyRowFills(tr, spec);
          refreshRowCitem(tr, spec);      // 归属合同定了 → 下拉收敛到这一张
        }
        applyRowLinks(tr, spec);
        recalcExpr(tr, spec);
        recalcSum(box);
      });
    }

    function addRow(box, row) {
      var spec = box.spec;
      var tr = document.createElement("tr");
      var html = "";
      spec.cols.forEach(function (col) {           // data-label 给手机端的卡片布局用
        html += '<td data-label="' + escapeHtml(col.label || "") + '">' +
          cellHtml(spec, col, row) + "</td>";
      });
      html += '<td><button class="btn btn-sm btn-danger dt-del" type="button" ' +
        'title="删除本行">×</button></td>';
      tr.innerHTML = html;
      box.tbody.appendChild(tr);
      wireRow(tr, box);
      applyRowFills(tr, spec);
      applyRowLinks(tr, spec);
      recalcRow(tr, spec);
      recalcExpr(tr, spec);
      recalcSum(box);
      return tr;
    }

    var boxes = {};
    document.querySelectorAll("[data-detail]").forEach(function (sec) {
      var spec = JSON.parse(sec.dataset.detail);
      var box = {
        spec: spec,
        el: sec,
        tbody: sec.querySelector("tbody"),
        sumBox: document.querySelector('[data-detail-sum="' + spec.key + '"]')
      };
      boxes[spec.key] = box;
      // 已有明细是服务端渲染好的：只挂事件，不重新渲染（免得 JS 出问题把明细弄丢）
      box.tbody.querySelectorAll("tr").forEach(function (tr) {
        wireRow(tr, box);
        // 打开页面就先把联动跑一遍：历史数据里若有「不含税 + 13%」，
        // 显示成 13% 会让用户以为保存的就是 13%，其实服务端会钉成 0%。
        applyRowLinks(tr, box.spec);
        recalcExpr(tr, box.spec);
      });
      box.tbody.addEventListener("click", function (ev) {
        var btn = ev.target.closest(".dt-del");
        if (!btn) return;
        ev.preventDefault();
        var tr = btn.closest("tr");
        if (tr) tr.remove();
        recalcSum(box);
      });
      recalcSum(box);
    });

    window.addDetailRow = function (key) {
      var box = boxes[key];
      if (box) addRow(box, {});
    };

    function labelOf(spec, name) {
      var hit = (spec.cols || []).filter(function (c) { return c.name === name; })[0];
      return hit ? hit.label : name;
    }

    /* 明细表上方的批量条：只应用"在这里主动选过"的项，没选（空值）的原样不动。
       例：选 含税方式=含税 + 税点=9% → 一次性把所有行刷成 9%。
       刷完再过一遍列联动，所以「不含税」的行税点会被拉回 0%。 */
    window.applyBulkDetail = function (key) {
      var box = boxes[key];
      if (!box) return;
      var note = document.querySelector('[data-bulk-note="' + key + '"]');
      var picks = {};
      box.el.querySelectorAll("[data-bulk]").forEach(function (sel) {
        if (sel.value !== "") picks[sel.dataset.bulk] = sel.value;
      });
      var names = Object.keys(picks);
      if (!names.length) {
        if (note) note.textContent = "先在左边填/选好要统一成的值，再点这个按钮。";
        return;
      }
      // 百分比列要在提示里补个 %（框里只放数字）
      function bulkType(n) {
        var list = box.spec.bulk || [];
        for (var i = 0; i < list.length; i++) {
          if (list[i].name === n) return list[i].type || "";
        }
        return "";
      }
      var rows = box.tbody.querySelectorAll("tr");
      if (!rows.length) {
        if (note) note.textContent = "还没有明细行可改，先点右上角【+ 添加一行】。";
        return;
      }
      var fixed = 0;
      rows.forEach(function (tr) {
        names.forEach(function (name) {
          var el = tr.querySelector('[name="d_' + key + "_" + name + '"]');
          if (el) el.value = picks[name];
        });
        applyRowLinks(tr, box.spec);
        recalcExpr(tr, box.spec);
        names.forEach(function (name) {
          var el = tr.querySelector('[name="d_' + key + "_" + name + '"]');
          if (el && el.value !== picks[name]) fixed++;   // 被联动规则修正了
        });
      });
      recalcSum(box);
      if (note) {
        note.textContent = "已把 " + rows.length + " 行改成：" +
          names.map(function (n) {
            return labelOf(box.spec, n) + " = " + picks[n] +
              (bulkType(n) === "pct" ? "%" : "");
          }).join("、") +
          (fixed ? "（其中 " + fixed + " 处被联动规则改过）" : "");
      }
    };

    // 关联合同勾选面板（对私计算）：筛选 / 计数 / 勾完刷新明细里的清单项下拉
    (function initContractPicker() {
      var picker = document.getElementById("contractPicker");
      if (!picker) return;
      var qEl = picker.querySelector("[data-chk-filter]");
      var nEl = picker.querySelector("[data-chk-count]");
      var items = Array.prototype.slice.call(picker.querySelectorAll(".chk-item"));

      function refresh() {
        var kw = qEl ? qEl.value.trim().toLowerCase() : "";
        var n = 0;
        items.forEach(function (it) {
          var box = it.querySelector("input");
          var hit = !kw || it.textContent.toLowerCase().indexOf(kw) >= 0;
          it.style.display = hit ? "" : "none";
          if (box && box.checked) n++;
        });
        if (nEl) nEl.textContent = n ? ("已选 " + n + " 张合同") : "还没勾合同";
      }
      if (qEl) qEl.addEventListener("input", refresh);
      items.forEach(function (it) {
        var box = it.querySelector("input");
        if (box) box.addEventListener("change", function () { refresh(); refreshAllCitems(); });
      });
      refresh();
    })();

    // 【按勾选的合同带出清单】：把勾好的合同拼成查询串打开新建页
    window.prefillFromChecked = function (btn) {
      var ids = headerContracts();
      if (!ids.length) {
        window.alert("先在上面勾选要带出清单的合同。");
        return;
      }
      window.location.href = btn.dataset.prefillHref + "?contract_ids=" + ids.join(",");
    };

    // 主表【关联合同】一变，清单项下拉的可选范围跟着换
    if (form) {
      var mEl = form.querySelector('[name="' + masterKey + '"]');
      if (mEl) {
        mEl.addEventListener("change", function () {
          Object.keys(boxes).forEach(function (k) {
            boxes[k].tbody.querySelectorAll(".dt-citem").forEach(function (sel) {
              var old = sel.value;
              sel.innerHTML = citemOptions({ contract_item_id: old });
              if (!sel.value) sel.value = "";
            });
          });
        });
      }
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("mainform");
    if (form) {
      if (form.dataset.calcGross) initTaxCalc(form);
      if (form.dataset.settle) initSettle();
      if (form.dataset.autoCalc) initAutoCalc(form);
      if (document.getElementById("detailSpecs")) initDetails();
    }
  });
})();

/* ------------------------------------------------ 发票 PDF 拖放导入
   拖进来就自动提交；同时把整块点一下 = 打开文件选择框。 */
(function () {
  var dz = document.getElementById('pdfDrop');
  var input = document.getElementById('pdfDropInput');
  var form = document.getElementById('pdfImportForm');
  var pick = document.getElementById('pdfPick');
  if (!dz || !input || !form) return;

  function submitFiles() {
    if (!input.files || !input.files.length) return;
    dz.classList.add('is-over');
    var tx = dz.querySelector('.dz-text b');
    if (tx) tx.textContent = '正在读取 ' + input.files.length + ' 张发票，请稍候…';
    Array.prototype.forEach.call(dz.querySelectorAll('button'), function (b) {
      b.disabled = true;
    });
    form.submit();
  }

  ['dragenter', 'dragover'].forEach(function (ev) {
    dz.addEventListener(ev, function (e) {
      e.preventDefault();
      e.stopPropagation();
      dz.classList.add('is-over');
    });
  });
  ['dragleave', 'drop'].forEach(function (ev) {
    dz.addEventListener(ev, function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (ev === 'dragleave' && dz.contains(e.relatedTarget)) return;
      dz.classList.remove('is-over');
    });
  });
  dz.addEventListener('drop', function (e) {
    var dt = e.dataTransfer;
    if (!dt || !dt.files || !dt.files.length) return;
    try {
      input.files = dt.files;              // 现代浏览器支持，直接接管拖进来的文件
    } catch (err) {
      alert('这个浏览器不支持拖拽上传，请点【选择文件…】');
      return;
    }
    submitFiles();
  });
  dz.addEventListener('click', function (e) {
    if (e.target.tagName !== 'BUTTON') input.click();
  });
  if (pick) pick.addEventListener('click', function (e) {
    e.preventDefault();
    input.click();
  });
  input.addEventListener('change', submitFiles);
})();

/* -------------------------------------------- 字段级联动（meta 里的 f.sets）
   声明式：meta 里给字段写 sets={"已支付": {"paid_amount": "@amount"}}，
   选了这个下拉就把目标字段设成对应值；值以 @ 开头 = 取那个字段当前的值。
   例：费用的「支付状态」选「已支付」→「已付金额」自动等于「金额」。
   打开页面时也跑一次，让两个字段与库里的值对齐。 */
(function () {
  var selects = document.querySelectorAll('select[data-sets]');
  if (!selects.length) return;

  function applyOne(sel, touched) {
    var map;
    try { map = JSON.parse(sel.getAttribute('data-sets') || '{}'); }
    catch (e) { return; }
    var rules = map[sel.value];
    if (!rules) return;
    Object.keys(rules).forEach(function (name) {
      var el = document.getElementById('f_' + name);
      if (!el) return;
      var v = rules[name];
      if (typeof v === 'string' && v.charAt(0) === '@') {
        var src = document.getElementById('f_' + v.slice(1));
        if (!src) return;
        v = (src.value === '' ? '0' : src.value);
      }
      el.value = v;
      if (touched) {
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
      }
    });
  }

  Array.prototype.forEach.call(selects, function (sel) {
    sel.addEventListener('change', function () { applyOne(sel, true); });
    applyOne(sel, false);
  });
})();

/* --------------------------- 下选项跟随其它字段（meta 里的 f.options_by）
   支持两种收窄，可叠加：
     1) 按另一个字段的取值分支：field + map（例：发票方向 → 只列同方向的合同）
     2) 按「项目」收窄：project_field + project_map（合同 → 项目 的映射）
        —— 先选【所属项目】，合同下拉就只列这个项目的合同。
   触发器一改就重建选项；当前值不在新集合里会被清空并提示。
   前端只是让人别选错；真正的口径由服务端兜（app._prepare_data 里还会校验一次）。 */
(function () {
  var sels = document.querySelectorAll('select[data-options-by]');
  if (!sels.length) return;

  Array.prototype.forEach.call(sels, function (sel) {
    var cfg;
    try { cfg = JSON.parse(sel.getAttribute('data-options-by') || '{}'); }
    catch (e) { return; }
    var trig = cfg.field ? document.getElementById('f_' + cfg.field) : null;
    var pfield = cfg.project_field
      ? document.getElementById('f_' + cfg.project_field) : null;
    if (!trig && !pfield) return;

    // 先把全部选项留一份底，来回切换时能还原
    var all = [];
    Array.prototype.forEach.call(sel.options, function (o) {
      all.push({ value: o.value, label: o.textContent });
    });

    var hint = sel.parentNode ? sel.parentNode.querySelector('.hint') : null;
    var baseHint = hint ? hint.textContent : '';
    if (hint) hint.setAttribute('data-base-hint', baseHint);

    function projectOk(v) {
      if (!pfield || !cfg.project_map) return true;
      var pid = pfield.value;
      if (!pid) return true;                 // 还没选项目：先全列出来
      var cpid = cfg.project_map[String(v)];
      return cpid !== null && cpid !== undefined && String(cpid) === String(pid);
    }

    function build() {
      var allow = (trig && cfg.map) ? cfg.map[trig.value] : null;
      var want = sel.value;

      sel.innerHTML = '';
      all.forEach(function (o) {
        if (o.value !== '') {
          if (allow && allow.indexOf(o.value) < 0) return;
          if (!projectOk(o.value)) return;
        }
        var op = document.createElement('option');
        op.value = o.value;
        op.textContent = o.label;
        sel.appendChild(op);
      });

      if (want && sel.querySelector('option[value="' + want + '"]')) {
        sel.value = want;
        if (hint) hint.textContent = baseHint;
      } else if (want) {
        sel.value = '';
        if (hint) {
          hint.textContent = '原来选的合同跟当前的项目 / 方向对不上，已清空；'
            + '请先选项目，再从这个项目的合同里选（也可以留空）。';
        }
      } else if (hint) {
        hint.textContent = baseHint;
      }
    }

    if (trig) trig.addEventListener('change', build);
    if (pfield) pfield.addEventListener('change', build);
    build();
  });
})();

/* 顶栏高度写进 CSS 变量 --topbar-h：
   手机上的侧栏抽屉是按顶栏底边定位的（CSS 里写死 58px 的话，
   顶栏万一行数变了、抽屉就会盖住/错位）。这里量一次真实高度兜住。 */
(function () {
  function syncTopbarHeight() {
    var bar = document.querySelector('.topbar');
    if (bar) {
      document.documentElement.style.setProperty(
        '--topbar-h', bar.offsetHeight + 'px');
    }
  }
  syncTopbarHeight();
  window.addEventListener('resize', syncTopbarHeight);
  window.addEventListener('orientationchange', syncTopbarHeight);
  if (document.fonts && document.fonts.ready) {
    // 字体加载完可能改变高度，再量一次
    document.fonts.ready.then(syncTopbarHeight);
  }
})();

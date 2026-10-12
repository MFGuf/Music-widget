"use strict";

// Общие помощники набора проверок. Набор грузится в iframe и общается с
// оркестратором через parent.check / parent.suiteDone.
//
// Дублируем каждую проверку в видимый текст набора: так тот же набор можно
// открыть в обычном браузере или снять скриншотом и сразу увидеть, что сломалось.

var __lines = [];

function paint() {
  var box = document.getElementById("report");
  if (!box) {
    box = document.createElement("pre");
    box.id = "report";
    box.style.cssText =
      "font:14px/1.5 monospace;background:#111;color:#ddd;padding:12px;margin:0;white-space:pre-wrap";
    document.body.appendChild(box);
  }
  box.textContent = __lines.join("\n");
}

function assert(name, cond, detail) {
  parent.check(name, !!cond, cond ? "" : (detail || "условие не выполнено"));
  __lines.push((cond ? "ok   " : "FAIL ") + name + (cond ? "" : "  <- " + (detail || "")));
  paint();
}

function assertEqual(name, actual, expected) {
  var a = JSON.stringify(actual);
  var b = JSON.stringify(expected);
  assert(name, a === b, a === b ? "" : ("получено " + a + ", ожидалось " + b));
}

function fail(name, err) {
  var text = String((err && err.stack) || err);
  assert(name, false, text);
}

function note(text) {
  __lines.push("      " + text);
  paint();
}

function done() {
  parent.suiteDone(null);
}

function doneError(message) {
  parent.suiteDone(String(message));
}

window.addEventListener("error", function (ev) {
  __lines.push("FAIL необработанная ошибка: " + ev.message + " @ " + ev.filename + ":" + ev.lineno);
  paint();
  parent.suiteDone("необработанная ошибка: " + ev.message);
});
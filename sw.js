// Service worker del CRM Arte.
// Serve a due cose: far funzionare l'app installata sul telefono, e mostrare
// le notifiche dei messaggi anche quando il CRM e' CHIUSO.
//
// La sveglia che arriva dal server NON contiene testo (scelta voluta: il
// testo dei messaggi non esce dal CRM). Quindi qui, appena svegliati,
// chiediamo al CRM quanti messaggi ci sono e scriviamo la notifica.
// Se la sessione e' scaduta e non riusciamo a chiederlo, mostriamo comunque
// un avviso generico: meglio "hai un messaggio" che nessuna notifica.

self.addEventListener("install", function (e) { self.skipWaiting(); });
self.addEventListener("activate", function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener("fetch", function (e) { });

self.addEventListener("push", function (event) {
  event.waitUntil((async function () {
    var corpo = "Apri il CRM per leggere.";
    var titolo = "CRM Arte — messaggio nuovo";
    try {
      var r = await fetch("/api/messaggi?conteggio=1", { credentials: "include" });
      if (r.ok) {
        var j = await r.json();
        var quanti = 0;
        if (j && j.titolare) {
          var d = j.da_leggere || {};
          var nomi = Object.keys(d);
          nomi.forEach(function (k) { quanti += d[k]; });
          if (quanti > 0) {
            titolo = quanti + " messaggio" + (quanti > 1 ? "i" : "") + " da leggere";
            corpo = "Da: " + nomi.join(", ");
          }
        } else if (j) {
          quanti = j.da_leggere || 0;
          if (quanti > 0) {
            titolo = quanti + " messaggio" + (quanti > 1 ? "i" : "") + " dal titolare";
            corpo = "Apri il CRM per leggere.";
          }
        }
        if (quanti === 0) {
          // niente da leggere: probabilmente e' gia' stato letto altrove.
          titolo = "CRM Arte";
          corpo = "Tutto letto.";
        }
      }
    } catch (e) { }
    return self.registration.showNotification(titolo, {
      body: corpo,
      icon: "/icona-192.png",
      badge: "/icona-192.png",
      tag: "crm-messaggi",          // una sola notifica, non una pila
      renotify: true,
      data: { apri: "/?messaggi=1" }
    });
  })());
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var vaiA = (event.notification.data && event.notification.data.apri) || "/?messaggi=1";
  event.waitUntil((async function () {
    var lista = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (var i = 0; i < lista.length; i++) {
      // Il CRM e' gia' aperto da qualche parte: lo porto in primo piano e gli
      // dico di aprire i messaggi, invece di aprire una seconda finestra.
      if (lista[i].url.indexOf(self.registration.scope) === 0) {
        try { lista[i].postMessage({ tipo: "apri-messaggi" }); } catch (e) { }
        return lista[i].focus();
      }
    }
    return self.clients.openWindow(vaiA);
  })());
});

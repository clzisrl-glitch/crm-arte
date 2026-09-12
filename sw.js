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
    var idScheda = "";
    try {
      // "anteprima=1": chiediamo anche il testo dell'ultimo messaggio non
      // letto, per mostrarlo nella notifica. Il server lo manda solo se la
      // sessione e' valida: se il cookie non c'e' piu', la notifica resta
      // generica invece di mostrare il testo a chi passa davanti al telefono.
      var r = await fetch("/api/messaggi?conteggio=1&anteprima=1", { credentials: "include" });
      if (r.ok) {
        var j = await r.json();
        var quanti = 0;
        var a = j && j.anteprima;
        if (j && j.titolare) {
          var d = j.da_leggere || {};
          var nomi = Object.keys(d);
          nomi.forEach(function (k) { quanti += d[k]; });
          if (quanti > 0) {
            titolo = (a && a.autore ? a.autore : "Telefoniste")
                   + (quanti > 1 ? " (+" + (quanti - 1) + ")" : "");
            corpo = (a && a.testo) ? a.testo : ("Da: " + nomi.join(", "));
          }
        } else if (j) {
          quanti = j.da_leggere || 0;
          if (quanti > 0) {
            titolo = (a && a.autore ? a.autore : "Il titolare")
                   + (quanti > 1 ? " (+" + (quanti - 1) + ")" : "");
            corpo = (a && a.testo) ? a.testo : "Apri il CRM per leggere.";
          }
        }
        if (a && a.id_contatto) idScheda = String(a.id_contatto);
        if (quanti === 0) {
          // niente da leggere: probabilmente e' gia' stato letto altrove.
          titolo = "CRM Arte";
          corpo = "Tutto letto.";
        }
      }
    } catch (e) { }
    // L'APP E' NOSTRA: se una pagina del CRM e' ancora viva (aperta, o in
    // secondo piano con lo schermo acceso) le diciamo di suonare il NOSTRO
    // suono e di aggiornare il pallino. Col CRM del tutto chiuso non c'e'
    // nessuna pagina a cui dirlo, e il suono resta quello di Android: non
    // esiste modo, da codice web, di sceglierlo noi.
    try {
      var vive = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (var i = 0; i < vive.length; i++) {
        try { vive[i].postMessage({ tipo: "suona-messaggio" }); } catch (e) { }
      }
    } catch (e) { }
    return self.registration.showNotification(titolo, {
      body: corpo,
      icon: "/icona-192.png",
      badge: "/icona-192.png",
      // SUONO E VIBRAZIONE. Attenzione: il suono lo decide ANDROID, non la
      // pagina web — l'opzione "sound" esiste nelle specifiche ma nessun
      // browser la applica. Quello che possiamo fare e' chiedere che la
      // notifica NON sia silenziosa e far vibrare. Il suono e il volume si
      // cambiano nelle impostazioni Android delle notifiche del sito.
      silent: false,
      vibrate: [220, 120, 220],
      // "tag" + "renotify" insieme sono la parte importante: senza renotify
      // il secondo messaggio aggiornerebbe la notifica IN SILENZIO, e non te
      // ne accorgeresti. Con renotify ogni messaggio nuovo suona di nuovo.
      tag: "crm-messaggi",          // una sola notifica, non una pila
      renotify: true,
      // sul computer la notifica resta finche' non la si tocca: se ti allontani
      // dalla scrivania non la perdi. Su Android viene ignorato.
      requireInteraction: true,
      timestamp: Date.now(),
      // Il clic porta ai messaggi; se il messaggio parlava di una scheda,
      // il CRM la apre direttamente.
      data: { apri: "/?messaggi=1" + (idScheda ? "&scheda=" + encodeURIComponent(idScheda) : "") }
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

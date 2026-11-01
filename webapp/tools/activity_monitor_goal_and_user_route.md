# Activity Monitor: doel, controles en gebruikersroute

Dit document bewaakt wat de Activity Monitor moet doen bij de verdere ontwikkeling in PCM Manager. Het beschrijft het gedrag van de bestaande Arie-tool zoals dat in de broncode is terug te vinden; het is geen voorstel om alle lokale technische details ongewijzigd naar een online omgeving te kopiëren.

## Doel

De Activity Monitor verzamelt en ordent bewijs van recente koersdeelname per renner uit WorldDB. Zo kan een gebruiker zien wanneer een renner voor het laatst gekoerst heeft, welke bron dat bewijst, welke controles nog nodig zijn en welke dossiers om aandacht vragen. De gebruiker kan op basis daarvan een renner handmatig beoordelen en besluiten of die in WorldDB blijft.

De monitor neemt zelf geen verwijderbesluit. Een ontbrekende uitslag, mislukte aanvraag of onzekere identiteit is geen bewijs dat iemand inactief is. Alleen uitslagen met bevestigde deelname tellen mee als koersactiviteit. De uitkomst is dus een controleerbaar overzicht voor menselijke beoordeling, geen automatische opschoning van de CDB.

## Bronnen en controles

### WorldDB/CDB-roster

- De gebruiker kiest de oorspronkelijke `.cdb` en synchroniseert de renners naar de monitor.
- De CDB wordt voor het inlezen als bron behandeld; de oorspronkelijke file blijft onaangeraakt. De monitor bewaart een momentopname en de historie, zodat rosterwijzigingen gevolgd kunnen worden.
- De monitor bewaart WorldDB-renner-ID, naam, geboortedatum, team, nationaliteit en FirstCycling-ID. De CDB-renner wordt bij export via de unieke FirstCycling-ID gekoppeld; de WorldDB-ID wordt niet in de CDB opgeslagen.
- PCS-profielslugs worden nooit uit `DYN_cyclist.CONSTANT` ingelezen. Bestaande `CONSTANT`-waarden blijven bij import buiten de Activity Monitor-profielkoppeling.
- Ontbrekende of ongeldige FirstCycling-ID’s worden niet als bruikbare ID behandeld. Identiteitswijzigingen kunnen een controlepauze veroorzaken in plaats van bewijs aan de verkeerde identiteit te koppelen.

### FirstCycling

- De FC-verzameling haalt gegevens op via de ingestelde FirstCycling API-toegang en gebruikt de FC-ID uit de CDB-roster.
- De collector bewaart afzonderlijk aanvraagpogingen, HTTP-/netwerkstatus, gedownloade gegevens en resultaten. Succes en dekking zijn dus verschillende zaken: een technisch geslaagde aanvraag betekent niet automatisch dat elk seizoen volledig is afgedekt.
- Verzoeken worden vertraagd en in begrensde batches uitgevoerd. Bij toegangsproblemen, rate limiting of herhaalde server-/netwerkfouten wordt de bron gepauzeerd; onafgemaakte controles blijven hervatbaar.
- De huidige workflow slaat FC over voor de Free Agent-teamgroep; die wordt via PCS gecontroleerd. De gebruiker kan ook expliciet PCS-only kiezen.

### ProCyclingStats

- PCS wordt rechtstreeks als HTML-profiel opgehaald met een normale HTTPS-aanvraag. De parser leest de profielgegevens en de `rdrResults`-resultatentabel; hij schrijft zelf geen gegevens naar de CDB.
- Resultaatrijen worden onderscheiden in eendaagse uitslag, etappe-uitslag, klassement, DNS en overige statussen. Een genummerde uitslag of DNF/OOT/OTL met een geldige datum kan bevestigde deelname zijn. DNS, DSQ/AB/HD, alleen klassementregels en niet-herkende regels tellen niet als bevestigde deelname.
- Een ontbrekende of niet-herkende resultatentabel is een parse-/dekkingsprobleem, niet “geen activiteit”. Een profiel dat niet bestaat, een serverfout of een onzekere identiteit wordt apart vastgelegd.
- Bij een geldige identiteit worden de gevonden rijen, datum, race, uitslag, bron en controlekoppeling opgeslagen. PCS-dekking blijft expliciet gedeeltelijk; de monitor presenteert haar niet als volledige seizoensdekking.

## PCS-link: vinden, invoeren en vertrouwen

De monitor kiest voor een controle de eerste beschikbare link in deze volgorde:

1. Een handmatige PCS-linkcorrectie of expliciet goedgekeurde profielkoppeling in de monitor.
2. Een eerder automatisch geverifieerd PCS-profiel voor dezelfde renneridentiteit.
3. Een aangenomen kandidaat-URL, gemaakt door de naam van de renner om te zetten naar een PCS-slug.

De aangenomen URL is alleen een zoek-/ophaalkandidaat. De tool kan hem openen en automatisch proberen op te halen, maar behandelt hem niet als bewijs dat het profiel bij de renner hoort. Als de pagina iemand anders beschrijft, de geboortedatum botst of de identiteit onvoldoende te controleren is, importeert de tool geen uitslag als bevestigde activiteit en zet ze het dossier in Problems.

De normale identiteitscontrole vergelijkt de PCS-profielnaam en geboortedatum met WorldDB; nationaliteit biedt aanvullende context. Als PCS geen geboortedatum toont, kan verificatie alleen via overeenkomende nationaliteit én bevestigde deelname in het huidige jaar. Een verschil in geboortedatum kan alleen handmatig worden goedgekeurd voor precies die PCS-link en precies die twee opgeslagen geboortedata; bij wijziging moet opnieuw worden gecontroleerd.

Een gebruiker kan de PCS-slug direct in de Riders-tabel corrigeren of in Rider Details een volledige PCS-profiel-URL opslaan. De link wordt genormaliseerd en moet een geldige HTTPS PCS-rennerpagina zijn; race- en uitslagpagina’s zijn geen profiel-link. Daarna kan de link opnieuw gecontroleerd worden. Verwijderen van de handmatige correctie laat geen CDB-link terugkomen; de gebruiker kiest daarna opnieuw een link of gebruikt de automatisch gevormde kandidaat.

Een handmatig gekozen link is een expliciete correctie van de gebruiker; een automatisch aangenomen kandidaat wordt nooit stilzwijgend als gecontroleerde identiteit opgeslagen. Na een succesvolle identiteitscontrole wordt de canonieke PCS-link als verificatiehistorie bewaard. Een recente controle op dezelfde link wordt normaal hergebruikt; de standaardverversing is 30 dagen, behoudens noodzakelijke parser-hercontrole.

Bij export maakt de tool een nieuwe `.cdb`; ze overschrijft de oorspronkelijke file niet. Alleen een PCS-link met een groen vinkje (bevestigd) of oranje `=` (handmatig aangepast of expliciet goedgekeurd na een geboorte datumverschil) wordt als slug in `DYN_cyclist.CONSTANT` gezet. De match gebeurt via een unieke FirstCycling-ID in de monitor en de CDB. Bij ontbrekende of dubbele matches blijft de bestaande `CONSTANT`-waarde staan. Alle overige bestaande waarden blijven eveneens ongemoeid.

## Wanneer is een controle klaar?

- FC en PCS hebben afzonderlijke voortgang en historie. In de normale gecombineerde route wordt voor een geselecteerde renner eerst FC geprobeerd en daarna PCS; een FC-resultaat onderdrukt de PCS-controle van die ronde niet.
- Elke bron kan afzonderlijk afgerond zijn. Een onderbroken ronde bewaart afgeronde resultaten en laat nog niet-afgeronde broncontroles open voor hervatting.
- Er bestaan handmatige selectie, reguliere automatische selectie, PCS-only en gerichte PCS-herstelpogingen voor eerder afgewezen identiteiten.
- De reguliere selectie neemt achterstallige/onafgeronde broncontroles eerst mee, daarna renners die volgens de monitoringregels aan de beurt zijn; resterende batchplekken kunnen naar de volgende geplande renners gaan.
- De intervalregels gebruiken de laatste bevestigde racedatum, controleschema, wachttijden en seizoens-/winterregels. Een technische poging zonder volledige controle wist de oorspronkelijke beoordelingsverplichting niet.

## Schermen en gebruikersroute

1. **Start / Activity overview** — toont het aantal gevolgde renners, wachtenden op een eerste controle, gecontroleerden, renners met bevestigde activiteit en dossiers die aandacht vragen. Toont ook voortgang en de laatste ronde. De introductie legt uit waarom de monitor bestaat.
2. **Riders** — zoek, filter en sorteer de volledige roster op status, team, nationaliteit en divisie. Open details om gevonden races en bronhistorie te bekijken; corrigeer indien nodig de PCS-link; selecteer renners voor een gerichte controle.
3. **Run checks** — kies een batch of geselecteerde renners, controleer de bronkeuze en vertraging, start of stop de ronde, en bekijk voortgang en samenvatting. Resultaten worden per stap bewaard.
4. **Problems** — behandel herstelbare dossierproblemen zoals niet-bestaande PCS-profielen, onzekere/verkeerde identiteit, ongeldige link en profiel-/rosterconflict. De actie is concreet: corrigeer de bron/link of identity in WorldDB en controleer opnieuw. Alleen werk dat een gebruiker kan oplossen hoort hier; algemene netwerkfouten en nog niet ingeplande eerste controles zijn geen identiteitsprobleem.
5. **Automated Checks** — opt-in achtergrondrondes via Windows Taakplanner. De gebruiker kiest 1–5 rondes per uur, dagvenster en minimale vertraging; de tool toont geplande tijden, voortgang, laatste samenvatting en biedt stoppen na de lopende aanvraag. De computer moet aan, wakker en aangemeld zijn. Gemiste rondes worden niet ingehaald en een parallelle ronde wordt overgeslagen.
6. **Settings & data** — database synchroniseren, instellingen aanpassen, gegevens back-uppen/herstellen en een nieuwe CDB-export maken.

De beoogde gebruikerslus is: **CDB synchroniseren → overzicht zien → controles uitvoeren of automatiseren → bewijs en problemen beoordelen → roster/PCS-link corrigeren waar nodig → opnieuw controleren → handmatig besluiten → desgewenst een nieuwe CDB exporteren.**

## Leidraad voor PCM Manager

- De bestaande navigatie en betekenisvolle schermen moeten één herkenbare Activity Monitor-werkstroom vormen; alleen een dashboardkaart of losse teller is niet genoeg.
- Resultaatbewijs, bronstatus, dekking, identiteitsstatus en technische fout zijn verschillende concepten en mogen niet tot één “checked/succeeded”-status worden samengevouwen.
- Toon de gebruiker waar de PCS-link vandaan komt: CDB, handmatig, automatisch geverifieerd of naamkandidaat.
- Maak een raceuitslag pas bruikbaar voor activiteit nadat de PCS-profielidentiteit is gecontroleerd. Bewaar reden en bronhistorie zodat een gebruiker de uitkomst kan narekenen.
- Houd monitoringhistorie en linkcorrecties gedeeld voor bevoegde gebruikers van de namespace, niet privé per account. Leg wel vast wie een handmatige correctie of beoordeling deed.
- Laat controles begrensd, vertraagd, hervatbaar en zichtbaar zijn. Stoppen voorkomt nieuwe aanvragen; opgeslagen resultaten blijven behouden.
- Verwijder niemand automatisch. “Potential retirement/removal candidate” is aanleiding voor menselijke beoordeling.
- Eerste PCM Manager-stap blijft de Activity Monitor-route met een gedeelde namespace-roster en bewijsmodel; pas daarna checks, schermen, problemen en planning één voor één aansluiten.

## Punten om vóór volledige online automatisering te besluiten

- De bestaande automatische ronde is lokaal aan een Windows-computer en gebruikerssessie gekoppeld. Een online toepassing heeft een server-side wachtrij/scheduler nodig; Windows Taakplanner en de “computer wakker en aangemeld”-aanname zijn daar niet geschikt voor.
- FirstCycling-toegang kan per deployment/account anders zijn. De bronvoorkeur en toegang moeten centraal en transparant ingesteld worden; het ontbreken ervan mag niet als “geen races” verschijnen.
- PCS wordt uit publieke HTML gelezen. Parserwijzigingen, toegangsblokkades en beperkte dekking moeten als expliciete bronstatus zichtbaar blijven; resultaten zonder herkenbare tabellen mogen niet als lege uitslag worden geïnterpreteerd.
- Het lokale model heeft veel historie en beoordelingsvelden. De online versie moet vastleggen welke gegevens gedeeld zijn en wie wijzigingen deed, terwijl het monitorresultaat zelf gedeeld blijft.

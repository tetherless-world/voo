from whyis import autonomic
from whyis.nanopub import Nanopublication
from whyis.datastore import create_id

from rdflib import *
import rdflib
from slugify import slugify
from whyis import nanopub
import os
import flask
from urllib.request import urlopen
from shutil import copyfileobj
from tempfile import TemporaryDirectory, mkdtemp

import re
import collections

import owlready2

from setlr import read_graph
from setlr.trig_store import TrigStore

from whyis.namespace import sioc_types, sioc, sio, dc, prov, whyis, NS
from rdflib.plugins.stores.sparqlstore import SPARQLStore

prefixes = dict(
  vann = Namespace('http://purl.org/vocab/vann/')
)
prefixes.update(NS.prefixes)

import_graph_query = '''
SELECT distinct ?ontology ?prefix ?label
WHERE {
#  GRAPH ?ontology_graph {

    {
      ?ontology a owl:Ontology.
    } union {
      ?ontology a skos:ConceptScheme.
    }
    optional {
      ?ontology rdfs:label ?label.
    }
    optional {
      ?ontology vann:preferredNamespaceUri ?prefix.
    }
#    minus { [] owl:imports ?ontology. }
    filter(!isblank(?ontology))
#  }
}
'''

def _create_portal_graph(portal):
    endpoint = portal.identifier
    store = SPARQLStore(query_endpoint=endpoint)
    return ConjunctiveGraph(store=store)

class ExtractOntologies(autonomic.UpdateChangeService):
    def getInputClass(self):
        return NS.local.OntologyPortal

    def getOutputClass(self):
        return NS.local.ImportGraphExtractedOntologyPortal

    def process(self, input, output):
        endpoint = input.value(NS.void.sparqlEndpoint)
        if endpoint is None:
            print("what endpoint?")
            return
        g = _create_portal_graph(endpoint)
        for ontology, uri_namespace, label in g.query(import_graph_query, initNs=prefixes):
            print ("Adding %s" % ontology)
            output.graph.add((ontology, NS.void.inDataset, input.identifier))
            if label is not None:
                output.graph.add((ontology, RDFS.label, label))
            if uri_namespace is not None:
                output.graph.add((ontology, prefixes['vann'].preferredNamespaceUri, uri_namespace))
            if (input.identifier, NS.rdf.type, NS.local.FullSPARQLDownload) in input.graph:
                output.graph.add((ontology, NS.void.sparqlEndpoint, endpoint.identifier))
            output.graph.add((ontology, RDF.type, NS.dcat.Distribution))
            output.graph.add((ontology, NS.void.inDataset, output.identifier))
        print("Finished extracting %s" % input.identifier)
    def explain(self, nanopub, i, o):
        autonomic.UpdateChangeService.explain(self, nanopub, i, o)
        nanopub.pubinfo.add((nanopub.assertion.identifier, NS.prov.wasQuotedFrom, o.identifier))

def load(url):
    with urlopen(url) as fsrc, TemporaryDirectory() as store_file:
        g = Graph(store="Oxigraph")
        g.store.open(store_file, create=True)
        from owlready2.owlxml_2_ntriples import parse as parseowlxml
        try:
            turtle = parseowlxml(fsrc)
            g.parse(data=turtle,format="turtle", publicID=NS.local)
            del turtle
            print("%s was parsed as owl/xml"% url)
        except Exception:
            try:
                g.parse(location=url,format='xml')
                print("%s was parsed as rdf/xml"% url)
            except Exception:
                try:
                    g.parse(location=url,format="turtle")
                    print("%s was parsed as turtle"% url)
                except Exception:
                    pass
    return g

def load_from_sparql(input, output):
    endpoint = input.value(NS.void.sparqlEndpoint)
    g = _create_portal_graph(endpoint)
    ontology_graph = Graph(identifier=input.identifier, store=g.store)
    other_ontologies = set([x for x in ontology_graph.subjects(NS.rdf.type, NS.owl.Ontology) if x != ontology])
    for s, p, o in ontology_graph:
        if s not in other_ontologies:
            output.graph.add((s, p, o))
    output.graph.add((ontology, NS.RDF.type, NS.owl.Ontology))
    print("Loaded %s triples from %s" % (len(output.graph), input.identifier))

class OntologyImporter(autonomic.GlobalChangeService):
    activity_class = whyis.OntologyImport
    
    def get_query(self):
        return '''
select distinct ?resource where {
    ?ontology owl:imports/prov:specializationOf? ?resource. 
    minus { ?resource a whyis:ImportedOntology. }
    minus { ?resource a owl:Ontology. }
}'''
    
    def getInputClass(self):
        return rdflib.OWL.Ontology

    def getOutputClass(self):
        return whyis.ImportedOntology

    def create_output_nanopub(self):
        storedir = mkdtemp()
        g = Graph(store="BerkeleyDB")
        g.store.open(storedir, create=True)
        id = create_id()
        nanopub = Nanopublication(identifier=flask.current_app.nanopub_manager.prefix[id], store=g.store)
        nanopub.nanopub_resource
        nanopub.assertion
        nanopub.provenance
        nanopub.pubinfo

        return nanopub
    
    def process_nanopub(self, input, output, nanopub):
        if (input.identifier, None, None) in flask.current_app.db:
            print("Ontology %s is already loaded."% input.identifier)
            return
        graph = load(input.identifier)
        print("Loaded %s with %s triples." % (input.identifier, len(graph)))        
        for ontology in graph.subjects(NS.rdf.type, NS.owl.Ontology):
            print("Main ontology is %s" % ontology)
            if output.identifier != ontology:
                nanopub.provenance.add((output.identifier, NS.prov.specializationOf, ontology))
        if len(list(graph.subjects(NS.RDF.type, NS.owl.Ontology))) == 0:
            output.add(NS.RDF.type, NS.owl.Ontology)
        for statement in graph:
            output.graph.add(statement)
        output.graph.commit()

class LoadOntology(autonomic.UpdateChangeService):
    def getInputClass(self):
        return NS.dcat.Distribution

    def getOutputClass(self):
        return NS.owl.Ontology

    def create_output_nanopub(self):
        storedir = mkdtemp()
        g = Graph(store="BerkeleyDB")
        g.store.open(storedir, create=True)
        id = create_id()
        nanopub = Nanopublication(identifier=flask.current_app.nanopub_manager.prefix[id], store=g.store)
        nanopub.nanopub_resource
        nanopub.assertion
        nanopub.provenance
        nanopub.pubinfo

        return nanopub
    
    def get_query(self):
        return '''
PREFIX voo: <http://purl.org/whyis/voo/>
select distinct ?resource where {
  ?resource a dcat:Distribution.
  minus { ?resource a owl:Ontology. }
  minus { ?resource prov:specializationOf [a owl:Ontology]. }
}'''

    def process_nanopub(self, input, output, nanopub):
        url = input.identifier
        if 'data.bioontology.org' in url:
            BIOPORTAL_API_KEY = os.getenv('BIOPORTAL_API_KEY')
            print("Added bioportal API key for %s." % url)
            url = url+ "?apikey="+BIOPORTAL_API_KEY
        elif 'rest.matportal.org' in url:
            MATPORTAL_API_KEY = os.getenv('MATPORTAL_API_KEY')
            print("Added Matportal API key for %s." % url)
            url = url+ "?apikey="+MATPORTAL_API_KEY
        if input.value(NS.void.sparqlEndpoint):
            load_from_sparql(input, output)
        else:
            graph = load(url)
            print("Loaded %s with %s triples." % (input.identifier, len(graph)))        
            load_ontology = True
            main_ontology = None
            for ontology in graph.subjects(NS.rdf.type, NS.owl.Ontology):
                main_ontology = ontology
                if output.identifier != ontology:
                    nanopub.provenance.add((output.identifier, NS.prov.specializationOf, ontology))
                    if (input.identifier, NS.rdf.type, NS.owl.Ontology) in flask.current_app.db:
                        print("Ontology %s is already loaded."% input.identifier)
                        load_ontology = False
            print("Main ontology is %s" % main_ontology)
            if len(list(graph.subjects(NS.RDF.type, NS.owl.Ontology))) == 0:
                output.add(NS.RDF.type, NS.owl.Ontology)
            if load_ontology:
                for statement in graph:
                    output.graph.add(statement)
        print("Output graph has %s statements for %s"%(len(output.graph), main_ontology))
        output.graph.commit()

primary_namespace_candidates = '''
PREFIX sio: <http://semanticscience.org/resource/>
prefix voo: <http://purl.org/whyis/voo/>

SELECT distinct 
  (max(?terms) as ?count) 
  ?namespace 
  ?alternate 
  (max(?altterms) as ?oterms)
WHERE {
  ?ontology sio:hasAttribute [
    a voo:NamespaceUseCount;
    sio:inRelationTo ?namespace;
    sio:hasValue ?terms
  ].
  ?altcount sio:inRelationTo ?namespace;
            a voo:NamespaceUseCount;
            sio:hasValue ?altterms.
  ?alternate sio:hasAttribute ?altcount.
} 
group by ?namespace ?alternate
having(?count > 1) 
order by desc(?count) desc(?oterms)
'''
        
class ComputePrimaryURINamespace(autonomic.GlobalChangeService):
    def getInputClass(self):
        return NS.local.NamespaceUseOntology

    def getOutputClass(self):
        return NS.local.PrimaryNamespaceOntology

    def get_query(self):
        return '''
prefix voo: <http://purl.org/whyis/voo/>
prefix void: <http://rdfs.org/ns/void#>
prefix vann: <http://purl.org/vocab/vann/>

select distinct ?resource where {
    ?resource a owl:Ontology .
    filter (!isblank(?resource))
    ?resource sio:hasAttribute [ a voo:NamespaceUseCount ].
    minus { ?resource prov:specializationOf ?ontology. }
    minus { ?resource vann:preferredNamespaceUri [] . }
#    minus { ?resource a voo:PrimaryNamespaceOntology. }
}'''
    
    def process(self, input, output):
        if (input.identifier, NS.sio.hasAttribute/NS.sio.inRelationTo, input.identifier) in input.graph:
            print("Giving %s the prefix of %s" % (input.identifier, input.identifier))
            output.add(prefixes['vann'].preferredNamespaceUri, output.identifier)
        else:
            namespaces = flask.current_app.db.query(primary_namespace_candidates,
                                                    initBindings={"ontology": input.identifier})
            namespaces_used = set()
            subset_of = None
            official_namespace = None
            for count, namespace, ontology, ontology_count in namespaces:
                if namespace in namespaces_used:
                    continue
                namespaces_used.add(namespace)
                print (count, namespace, ontology, ontology_count)
                if count >= ontology_count:
                    # This is clearly the namespace of the ontology.
                    official_namespace = namespace
                    break
                elif subset_of is None:
                    subset_of = ontology
            if official_namespace is not None:
                print("Giving %s the prefix of %s" % (input.identifier, official_namespace))
                output.add(prefixes['vann'].preferredNamespaceUri, official_namespace)
            elif subset_of is not None:
                print("Setting %s as a subset of %s" % (input.identifier, subset_of))
                output.graph.add((ontology, NS.void.subset, output.identifier))
            else:
                print("Cannot find a prefix for %s" % input.identifier)
                    
    
class ExtractOntologyURINamespace(autonomic.GlobalChangeService):
    def getInputClass(self):
        return NS.owl.Ontology

    def getOutputClass(self):
        return NS.local.NamespaceUseOntology

    def get_query(self):
        return '''
prefix voo: <http://purl.org/whyis/voo/>
prefix void: <http://rdfs.org/ns/void#>
prefix vann: <http://purl.org/vocab/vann/>

select distinct ?resource where {
  ?ontterm rdfs:subClassOf voo:Term.
  graph ?g {
      ?resource a owl:Ontology .
      ?class a ?ontterm.
  } 
    filter (!isblank(?resource))
    minus { ?resource prov:specializationOf ?ontology. }
#    minus { ?resource a voo:NamespaceUseOntology. }
}'''

    def process(self, i, o):
        classes = [row.asdict() for row in flask.current_app.db.query('''PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
PREFIX prov: <http://www.w3.org/ns/prov#>
prefix voo: <http://purl.org/whyis/voo/>
SELECT distinct ?class where {
  ?ontterm rdfs:subClassOf voo:Term.
  graph ?g {
      ?ontology a owl:Ontology .
      ?class a ?ontterm.
  } 
  filter(!isblank(?class))
}''', initBindings = dict(ontology=i.identifier))]
        namespaces = collections.defaultdict(set)
        print ('%s has %s classes'%(i.identifier, len(classes)))
        for c in classes:
            if "#" in c['class']:
                localpart = c['class'].split('#')[-1]
            else:
                localpart = re.split(r'[#_/]+',c['class'])[-1]
            ns = c['class'][:-len(localpart)]
            namespaces[ns].add(c['class'])
        #ns = sorted(namespaces.items(),key=lambda x: len(x[1]),reverse=True)
        #subset_ontology = None
        print("%s uses these namespaces:"%i.identifier)
        for namespace, classes in namespaces.items():
            # <o> sio:hasAttribute [
            #   a voo:NamespaceUseCount;
            #   sio:hasValue {{len(classes)}};
            #   sio:inRelationTo <namespace>
            # ].
            print("%s\t%s"%(namespace, len(classes)))
            nuc = o.graph.resource(BNode())
            nuc.add(NS.rdf.type, NS.local.NamespaceUseCount)
            nuc.add(NS.sio.hasValue, Literal(len(classes)))
            nuc.add(NS.sio.inRelationTo, URIRef(namespace))
            o.add(NS.sio.hasAttribute, nuc)


quoted_classes_query = '''
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
prefix voo: <http://purl.org/whyis/voo/>
prefix void: <http://rdfs.org/ns/void#>
prefix vann: <http://purl.org/vocab/vann/>

select distinct ?from (count (distinct ?class) as ?classes) where {
  ?ontterm rdfs:subClassOf voo:Term.
  graph ?g {
      ?ontology a owl:Ontology .
      ?class a ?ontterm.
  } 

  minus {
    graph ?g {
      [] owl:imports ?ontology.
    }
  }

  filter(!isblank(?class))
  ?ontology vann:preferredNamespaceUri ?ns.
  filter(!STRSTARTS(str(?class), str(?ns)))
  ?from vann:preferredNamespaceUri ?ns2.
  minus {
      ?ontology owl:imports+ ?from.
  }
  filter(STRSTARTS(str(?class), str(?ns2)))
} group by ?from
'''

quoted_class_properties_query = '''
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
prefix voo: <http://purl.org/whyis/voo/>
prefix void: <http://rdfs.org/ns/void#>
prefix vann: <http://purl.org/vocab/vann/>

select distinct ?property (count (distinct ?class) as ?classes) where {
  ?ontterm rdfs:subClassOf voo:Term.
  graph ?g {
    ?ontology a owl:Ontology.
    ?class a ?ontterm.
    ?class ?property [].
  }

  minus {
    graph ?g {
      [] owl:imports ?ontology.
    }
  }

  minus {
      ?ontology owl:imports+ ?imported.
      ?ontterm2 rdfs:subClassOf* voo:Term.
      ?class a ?ontterm2.
  }
  ?ontology vann:preferredNamespaceUri ?ns.
  filter(!STRSTARTS(str(?class), str(?ns)))
} group by ?property
'''

class LinkQuotedOntologies(autonomic.GlobalChangeService):
    def getInputClass(self):
        return NS.owl.NamespacedOntology
    
    def getOutputClass(self):
        return NS.local.InterlinkedOntology

    def get_query(self):
        return '''
prefix vann: <http://purl.org/vocab/vann/>
prefix voo: <http://purl.org/whyis/voo/>

select ?resource where {
    ?resource rdf:type/rdfs:subClassOf* voo:Ontology.
    ?resource vann:preferredNamespaceUri [].
}'''
    def process(self, i, o):
        for quoted_from, count in flask.current_app.db.query(quoted_classes_query,
                                                             initBindings={"ontology":i.identifier}):
            r = o.graph.resource(rdflib.BNode())
            r.add(rdflib.RDF.type,NS.local.MIREOTCount)
            r.add(NS.sio.hasValue, count)
            r.add(NS.sio.inRelationTo, quoted_from)
            o.add(NS.sio.hasAttribute, r)
        for p, count in flask.current_app.db.query(quoted_class_properties_query,
                                                   initBindings={"ontology":i.identifier}):
            r = o.graph.resource(rdflib.BNode())
            r.add(rdflib.RDF.type, NS.local.MIREOTPropertyCount)
            r.add(NS.sio.hasValue, count)
            r.add(NS.sio.inRelationTo, p)
            o.add(NS.sio.hasAttribute, r)
            

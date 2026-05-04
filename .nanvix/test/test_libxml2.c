/* Minimal libxml2 integration test for Nanvix. */
#include <libxml/parser.h>
#include <libxml/tree.h>

static const char *TEST_XML = "<root><child>text</child></root>";

int main(void) {
    xmlDocPtr doc;
    xmlNodePtr root;

    xmlInitParser();

    doc = xmlParseMemory(TEST_XML, 32);
    if (!doc) return 1;

    root = xmlDocGetRootElement(doc);
    if (!root) { xmlFreeDoc(doc); return 1; }

    xmlFreeDoc(doc);
    xmlCleanupParser();
    return 0;
}

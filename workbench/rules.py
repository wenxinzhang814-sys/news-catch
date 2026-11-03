"""Parse the user supplied Inoreader boolean expressions without eval()."""
import re
import unicodedata

def normalize(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value).casefold().replace('-', ' ')).strip()

def contains(text, term):
    term = normalize(term.strip('"'))
    return bool(re.search(r'(?<!\w)' + re.escape(term).replace(r'\ ', r'\s+') + r'(?!\w)', normalize(text)))

def tokens(expression):
    return re.findall(r'"[^"\n]+"|\(|\)|[^\s()]+', expression)

class RuleParser:
    def __init__(self, expression, text):
        self.items, self.i, self.text = tokens(expression), 0, text
    def peek(self):
        return self.items[self.i] if self.i < len(self.items) else ''
    def take(self):
        value = self.peek(); self.i += 1; return value
    def atom(self):
        token = self.take()
        if token == '(':
            value = self.or_expr()
            if self.take() != ')': raise ValueError('筛选表达式缺少右括号')
            return value
        if not token or token.upper() in ('AND', 'OR', 'NOT', ')'):
            raise ValueError('筛选表达式缺少关键词')
        return contains(self.text, token)
    def unary(self):
        if self.peek().upper() == 'NOT':
            self.take(); return not self.unary()
        return self.atom()
    def and_expr(self):
        value = self.unary()
        while self.peek() and self.peek() != ')' and self.peek().upper() != 'OR':
            if self.peek().upper() == 'AND': self.take()
            right = self.unary()
            value = value and right
        return value
    def or_expr(self):
        value = self.and_expr()
        while self.peek().upper() == 'OR':
            self.take(); right = self.and_expr(); value = value or right
        return value
    def parse(self):
        value = self.or_expr()
        if self.peek(): raise ValueError('筛选表达式存在多余内容')
        return value

def import_rules(path):
    text = path.read_text(encoding='utf-8-sig')
    result = []
    for match in re.finditer(r'###\s*(Feed\s*(\d+)[^\n]*)\n\n```\n([\s\S]*?)\n```', text):
        expression = re.sub(r'\s+', ' ', match[3]).strip()
        RuleParser(expression, '').parse()
        result.append(dict(name=match[1], expression=expression, scope='title_content' if match[2] == '4' else 'title'))
    if len(result) != 4: raise ValueError('指定文档中未解析出完整的 4 组 Feed 规则')
    return result

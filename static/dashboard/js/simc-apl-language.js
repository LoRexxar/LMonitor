import {StreamLanguage} from '../../vendor/codemirror/codemirror-6.0.1.bundle.js';

const keywords = /^(actions|actions\.[a-zA-Z0-9_]+|profileset|target_if)$/;
const operators = /^(if|target_if|name|value|value_else|value_if|op|sec|line_cd|cycle_targets|max_cycle_targets|interrupt|interrupt_if|use_off_gcd|use_while_casting|wait_on_ready)$/;
const booleans = /^(true|false)$/;

export const simcAplLanguage = StreamLanguage.define({
    startState: () => ({ afterSlash: false }),
    token(stream, state) {
        if (stream.sol()) state.afterSlash = false;
        if (stream.eatSpace()) return null;
        if (stream.peek() === '#') {
            stream.skipToEnd();
            return 'comment';
        }
        if (stream.match(/^(actions(?:\.[a-zA-Z0-9_]+)?)(?=\+?=)/)) return 'keyword';
        if (stream.match(/^\+=|^=/)) return 'operator';
        if (stream.match(/^\//)) {
            state.afterSlash = true;
            return 'operator';
        }
        if (stream.match(/^[|&!<>+*%()-]+/)) return 'operator';
        if (stream.match(/^\d+(?:\.\d+)?/)) return 'number';
        if (stream.match(/^[a-zA-Z_][a-zA-Z0-9_.]*/)) {
            const word = stream.current();
            if (state.afterSlash) {
                state.afterSlash = false;
                return 'variableName.function';
            }
            if (keywords.test(word)) return 'keyword';
            if (operators.test(word)) return 'propertyName';
            if (booleans.test(word)) return 'bool';
            return word.includes('.') ? 'variableName' : null;
        }
        stream.next();
        return null;
    },
    languageData: {
        commentTokens: {line: '#'},
        closeBrackets: {brackets: ['(', '[', '"']},
    },
});

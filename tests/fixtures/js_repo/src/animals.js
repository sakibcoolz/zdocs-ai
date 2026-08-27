const { Logger } = require('./logger');

class Animal {
  constructor(name) {
    this.name = name;
    this.logger = new Logger();
  }

  speak() {
    this.logger.log(this.name);
  }
}

class Dog extends Animal {
  speak() {
    this.logger.log(`${this.name} barks`);
  }
}

function makeDog(name) {
  return new Dog(name);
}

module.exports = { Animal, Dog, makeDog };
